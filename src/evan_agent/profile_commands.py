"""个人资料终端管理入口；所有命令本地执行，不进入模型对话历史。"""

import shlex
import sqlite3

from evan_agent.auth import AuthContext, require_owner
from evan_agent.knowledge import EntryNotFoundError, KnowledgeStore, ProfileEntry
from evan_agent.terminal import read_line

HELP = """个人资料管理：
/profile add              新增资料（本人）
/profile list [页码]       查看可见资料，每页 10 条
/profile show 资料ID       查看完整资料
/profile edit 资料ID       修改资料（本人）
/profile delete 资料ID     永久删除资料（本人，需确认）
/profile help             显示帮助
新增、修改过程中输入 /cancel 可取消；多行原文输入 /end 结束。
访客只能查看公开且已发布的资料；来源为手动录入。"""


class Cancelled(Exception):
    """用户取消当前管理操作。"""


def ask(prompt: str) -> str:
    """保留原始输入，仅把单独的 /cancel 识别为控制命令。"""
    value = read_line(prompt)
    if value.strip() == "/cancel":
        raise Cancelled
    return value


def content_input(*, editing: bool = False) -> str | None:
    """多行录入原文；编辑时第一行直接回车表示保留原文。"""
    print("输入资料原文，单独输入 /end 完成，/cancel 取消。" + ("首行直接回车保留原文。" if editing else ""))
    lines = []
    while True:
        line = ask("> ")
        if editing and not lines and line == "":
            return None
        if line.strip() == "/end":
            break
        lines.append(line)
    content = "\n".join(lines)
    if not content.strip():
        raise ValueError("资料内容不能为空，未保存。")
    return content


def choose(prompt: str, default: str, choices: dict[str, str]) -> str:
    """将中文或英文选项转成数据库枚举值，空输入采用当前／默认值。"""
    while True:
        value = ask(f"{prompt} [{default}]：").strip()
        if not value:
            return default
        if value in choices:
            return choices[value]
        print("选项无效，请输入：" + " / ".join(choices))


def show(entry: ProfileEntry) -> None:
    """显示已经通过存储层可见性校验的资料。"""
    print(f"ID：{entry.id}\n分类：{entry.category}\n来源：{entry.source}\n"
          f"公开范围：{entry.visibility}\n状态：{entry.status}\n"
          f"创建时间：{entry.created_at}\n更新时间：{entry.updated_at}\n原文：\n{entry.content}")


class ProfileCommands:
    """命令分发与交互确认；真正的写入授权仍在 KnowledgeStore 内检查。"""

    def __init__(self, store: KnowledgeStore):
        self.store = store

    def handle(self, command: str, identity: AuthContext) -> bool:
        """识别 /profile 命令；返回 True 表示已消费，禁止继续交给模型。"""
        if not command.split() or command.split()[0] != "/profile":
            return False
        try:
            parts = shlex.split(command)
            action = parts[1] if len(parts) > 1 else "help"
            args = parts[2:]
            if action == "help" and not args:
                print(HELP)
            elif action == "list" and len(args) <= 1:
                page = int(args[0]) if args else 1
                if page < 1:
                    raise ValueError("页码必须为正整数。")
                entries = self.store.list_entries(identity, limit=11, offset=(page - 1) * 10)
                if not entries:
                    print("本页暂无可见资料。本人可用 /profile add 新增资料。")
                for entry in entries[:10]:
                    preview = entry.content.replace("\n", " ")[:60]
                    print(f"{entry.id} | {entry.category} | {entry.visibility}/{entry.status}\n  {preview}")
                if len(entries) > 10:
                    print(f"还有更多资料：/profile list {page + 1}")
            elif action == "show" and len(args) == 1:
                show(self.store.get(identity, args[0]))
            elif action == "add" and not args:
                self._save(identity)
            elif action == "edit" and len(args) == 1:
                require_owner(identity)
                self._save(identity, self.store.get(identity, args[0]))
            elif action == "delete" and len(args) == 1:
                require_owner(identity)
                entry = self.store.get(identity, args[0])
                show(entry)
                if ask("永久删除以上资料？输入 删除 确认，其余输入取消：").strip() == "删除":
                    self.store.delete(identity, entry.id)
                    print("资料已永久删除。")
                else:
                    print("已取消删除。")
            else:
                print("命令格式不正确。\n" + HELP)
        except (Cancelled, EOFError, KeyboardInterrupt):
            print("\n已取消本次资料操作，未保存修改。")
        except (PermissionError, EntryNotFoundError, ValueError) as error:
            print(f"资料操作未完成：{error}")
        except sqlite3.Error:
            print("资料数据库操作失败，请检查本地文件权限或稍后重试。")
        return True

    def _save(self, identity: AuthContext, original: ProfileEntry | None = None) -> None:
        """先收集和预览所有字段，再一次性写入，取消不会留下半条资料。"""
        require_owner(identity)
        content = content_input(editing=original is not None)
        category = ask(f"分类 [{original.category if original else 'general'}]：").strip()
        fields = {
            "content": original.content if content is None and original else content,
            "category": category or (original.category if original else "general"),
            "visibility": choose("公开范围（private 仅本人 / public 公开）",
                                 original.visibility if original else "private",
                                 {"private": "private", "仅本人": "private", "public": "public", "公开": "public"}),
            "status": choose("状态（draft 草稿 / published 已发布 / archived 归档）",
                             original.status if original else "draft",
                             {"draft": "draft", "草稿": "draft", "published": "published", "已发布": "published", "archived": "archived", "归档": "archived"}),
        }
        # 输入过程中可能超过登录有效期；展示待保存内容前再次检查。
        require_owner(identity)
        print(f"待保存：{fields['category']} | {fields['visibility']}/{fields['status']}\n{fields['content']}")
        if fields['visibility'] == 'public' and fields['status'] == 'published':
            print("保存后访客可查看这条资料。")
        if ask("输入 保存 确认，其余输入取消：").strip() != "保存":
            print("已取消保存。")
            return
        # 编辑保留原来源；新增的资料来源明确标记为 manual。
        entry = self.store.update(identity, original.id, **fields) if original else self.store.create(identity, source="manual", **fields)
        print(f"资料已保存：{entry.id}")
