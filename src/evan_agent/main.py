"""Evan Agent 命令行入口：读取配置，连接 MiniMax，维护多轮对话。"""

import os
from pathlib import Path

from dotenv import load_dotenv
from anthropic import APIError

from evan_agent.auth import AuthContext, AuthenticationError, AuthStore, read_password
from evan_agent.sessions import SessionStore
from evan_agent.knowledge import KnowledgeStore
from evan_agent.profile_commands import ProfileCommands
from evan_agent.service import AgentService
from evan_agent.memory_review import MemoryReviewer, ReviewCancelled
from evan_agent.terminal import read_line, StreamingPrinter


def describe_api_error(error: APIError, api_key: str) -> str:
    """提取服务端错误原因，并隐藏错误消息中可能出现的当前密钥。

    Args:
        error: Anthropic 兼容接口抛出的 API 异常。
        api_key: 当前使用的 MiniMax 密钥，用于输出前脱敏。

    Returns:
        包含 HTTP 状态（或异常类型）和错误原因的中文提示。
    """
    # 网络错误不一定包含响应体；服务端错误通常提供结构化的 body。
    body = getattr(error, "body", None)
    detail = "请检查网络、Key、模型权限及账户额度。"
    if isinstance(body, dict):
        # 兼容 {"error": {"message": ...}} 和直接包含 message 的响应。
        payload = body.get("error", body)
        if isinstance(payload, dict):
            detail = str(payload.get("message") or detail)
        elif isinstance(payload, str):
            detail = payload
    # 不直接打印整个异常或请求头，避免把当前密钥带到终端输出中。
    detail = detail.replace(api_key, "[已隐藏]")
    # 连接失败可能没有 HTTP 状态码，此时显示异常类型。
    status = getattr(error, "status_code", None)
    return f"MiniMax 请求失败（{status or type(error).__name__}）：{detail}"


def main() -> None:
    """启动交互式聊天；对话历史保存在内存中，程序退出后清空。"""
    # 从启动命令所在目录读取 .env，因此应在项目根目录启动。
    # load_dotenv 默认不覆盖终端中已导出的同名环境变量。
    # .env 中的 NO_PROXY 也在此加载，让指定 MiniMax 域名绕过本机代理。
    load_dotenv(Path.cwd() / ".env")
    auth_store = AuthStore()
    knowledge = KnowledgeStore()
    profiles = ProfileCommands(knowledge)
    identity = AuthContext.guest()
    sessions = SessionStore()
    conversation_id = sessions.create(identity)
    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    # api_key鉴空。
    if not api_key or api_key == "your-minimax-key-here":
        raise SystemExit("请在项目根目录的 .env 中填写 MINIMAX_API_KEY（MiniMax API / 订阅 Key）。")

    service = AgentService(auth=auth_store, knowledge=knowledge, sessions=sessions)
    agent = service.agent
    model_name = (os.getenv("MODEL") or "MiniMax-M3.1-Flash").strip()

    # 完整消息历史由 SessionStore 按聊天 ID 保存，不再共用一个列表。
    print(f"Evan Agent 已启动（MiniMax / {model_name}）。输入 exit 或 quit 结束对话。")
    print("当前身份：访客。命令：/login 登录、/logout 退出登录、/whoami 查看身份、/help 帮助。")
    print(f"当前聊天：{conversation_id}。/new 新建、/sessions 列表、/switch ID 切换、/clear 清空。")
    print("个人资料管理：/profile help（本地保存，不调用模型）。")
    if not auth_store.is_initialized():
        print("尚未设置主人密码，可另开终端运行 uv run evan-auth init。")

    try:
        while True:
            # 去掉首尾空白；支持退出命令，忽略没有内容的输入。
            prompt = read_line("你：").strip()
            # 等待输入期间会话也可能过期；在处理任何命令或模型请求前降级并清空历史。
            if identity.role == "owner" and not identity.is_owner:
                sessions.discard_identity(identity)
                identity = AuthContext.guest()
                conversation_id = sessions.create(identity)
                print("本人登录已过期，已切换为访客并清空当前上下文。")
            if prompt.lower() in {"exit", "quit"}:
                break
            if not prompt:
                continue
            # 管理命令、录入内容及确认输入不进入消息历史或模型上下文。
            if profiles.handle(prompt, identity):
                continue
            # 认证命令在本地消费，命令、密码和认证结果均不进入模型消息历史。
            if prompt == "/login":
                sessions.discard_identity(identity)
                identity = AuthContext.guest()
                try:
                    identity = auth_store.login(read_password("主人密码："))
                except (AuthenticationError, EOFError, KeyboardInterrupt) as error:
                    print(str(error) or "已取消登录。")
                else:
                    print("本人登录成功，有效期 30 分钟。已开启新的对话会话。")
                conversation_id = sessions.create(identity)
                continue
            if prompt == "/logout":
                sessions.discard_identity(identity)
                identity = AuthContext.guest()
                conversation_id = sessions.create(identity)
                print("已退出登录，切换为访客并清空当前上下文。")
                continue
            if prompt == "/whoami":
                print(f"当前身份：{'本人' if identity.is_owner else '访客'}；登录会话：{identity.session_id}；聊天：{conversation_id}")
                continue
            if prompt == "/new":
                conversation_id = sessions.create(identity)
                print(f"已新建空白聊天：{conversation_id}")
                continue
            if prompt == "/sessions":
                for item in sessions.list_for(identity):
                    marker = "*" if item.conversation_id == conversation_id else " "
                    print(f"{marker} {item.conversation_id} | {item.message_count} 条消息")
                continue
            if prompt == "/clear":
                sessions.clear(identity, conversation_id)
                print("当前聊天已清空。")
                continue
            if prompt == "/switch" or prompt.startswith("/switch "):
                parts = prompt.split()
                if len(parts) != 2:
                    print("用法：/switch 聊天ID（通过 /sessions 查看完整 ID）")
                    continue
                try:
                    sessions.history(identity, parts[1])
                except PermissionError as error:
                    print(error)
                else:
                    conversation_id = parts[1]
                    print(f"已切换到聊天：{conversation_id}")
                continue
            if prompt == "/help":
                print("/login：本人登录\n/logout：退出并删除本次身份的全部聊天\n/whoami：查看身份\n/new：新建聊天\n/sessions：列出当前身份的聊天\n/switch ID：切换聊天\n/clear：清空当前聊天\n/profile help：个人资料管理\nexit 或 quit：退出程序")
                continue
            if prompt.startswith("/"):
                print("未知命令。请使用 /help；/login 后不要附加密码。")
                continue
            output = StreamingPrinter()
            try:
                # 流式显示主模型文本；完整消息仍在整轮成功后写入当前会话。
                answer = sessions.invoke(agent, identity, conversation_id, prompt, reviewer=MemoryReviewer(knowledge), on_text=output.write, on_message=output.new_message)
            except ReviewCancelled as error:
                output.finish()
                print(error)
                continue
            except PermissionError as error:
                output.finish()
                sessions.discard_identity(identity)
                identity = AuthContext.guest()
                conversation_id = sessions.create(identity)
                print(f"{error} 已切换到新的访客聊天。")
                continue
            except APIError as error:
                output.finish()
                if output.has_text:
                    print("回答传输中断，以上为未完成内容，本轮聊天历史未保存。")
                # API 请求失败后显示原因，允许用户继续输入或退出。
                print(describe_api_error(error, api_key))
                continue
            finally:
                output.finish()
            # 限额中间件的合成回复没有模型 token 流，仍需显示。
            if answer and answer != output.last_text:
                print(f"Agent：{answer}\n")
    except (EOFError, KeyboardInterrupt):
        # Ctrl+D（输入结束）或 Ctrl+C 均视为正常结束聊天。
        pass
    finally:
        # 无论通过退出命令还是终端中断结束，都显示告别提示。
        print("\n再见！")


if __name__ == "__main__":
    # 支持 python -m evan_agent.main；uv run evan-agent 则由项目脚本入口调用。
    main()
