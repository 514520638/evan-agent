"""Evan Agent 命令行入口：读取配置，连接 MiniMax，维护多轮对话。"""

import os
from pathlib import Path

from dotenv import load_dotenv
from anthropic import APIError
from langchain.agents import create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, HumanMessage


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
    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    # api_key鉴空。
    if not api_key or api_key == "your-minimax-key-here":
        raise SystemExit("请在项目根目录的 .env 中填写 MINIMAX_API_KEY（MiniMax API / 订阅 Key）。")

    # 使用 MiniMax 原始模型 ID；没有配置时使用默认模型。
    model_name = (os.getenv("MODEL") or "MiniMax-M3.1-Flash").strip()
    # ChatAnthropic 处理 Anthropic 协议；实际请求发送到下面的 MiniMax 地址。
    # 显式传入密钥和地址，避免误用其他供应商的默认认证配置。
    model = ChatAnthropic(
        model=model_name,
        api_key=api_key,
        base_url=os.getenv("MINIMAX_BASE_URL").strip(),
        max_tokens=8192,  # 单次生成上限，包含模型的思考 token。
        timeout=60,  # SDK HTTP 请求超时设置，单位为秒。
        max_retries=1,  # 对 SDK 判定可重试的请求，最多自动重试一次。
    )

    # 创建 LangChain Agent；后续可以在 tools 中注册业务工具。
    # system_prompt 用于设定助手的角色和回复风格。
    agent = create_agent(
        model=model,
        tools=[],
        system_prompt="你是 Evan，一位友好的 AI 助手。请清楚、简洁地回答用户问题。",
    )
    
    # 保留完整消息对象，以便回传文本、思考内容和未来的工具调用信息。
    messages: list[BaseMessage] = []
    print(f"Evan Agent 已启动（MiniMax / {model_name}）。输入 exit 或 quit 结束对话。")

    try:
        while True:
            # 去掉首尾空白；支持退出命令，忽略没有内容的输入。
            prompt = input("你：").strip()
            if prompt.lower() in {"exit", "quit"}:
                break
            if not prompt:
                continue
            try:
                # 把历史消息与本轮输入一起交给 Agent。
                # 此处构造新列表，请求失败时不把本轮输入写入已确认的历史。
                result = agent.invoke(
                    {"messages": [*messages, HumanMessage(content=prompt)]}
                )
            except APIError as error:
                # API 请求失败后显示原因，允许用户继续输入或退出。
                print(describe_api_error(error, api_key))
                continue
            # 请求成功后保存完整历史；终端仅显示最终消息的文本部分。
            messages = result["messages"]
            print(f"Agent：{messages[-1].text}\n")
    except (EOFError, KeyboardInterrupt):
        # Ctrl+D（输入结束）或 Ctrl+C 均视为正常结束聊天。
        pass
    finally:
        # 无论通过退出命令还是终端中断结束，都显示告别提示。
        print("\n再见！")


if __name__ == "__main__":
    # 支持 python -m evan_agent.main；uv run evan-agent 则由项目脚本入口调用。
    main()
