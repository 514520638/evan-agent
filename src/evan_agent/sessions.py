"""按聊天 ID 存储短期上下文，并将每段对话绑定到创建它的登录会话。

生产入口使用 SQLite 持久化，测试可继续使用纯内存模式。
模型输入不承担权限校验，所有历史读取与写入都在此模块检查身份。
"""

from copy import deepcopy
from contextlib import closing
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any
from uuid import uuid4

from langchain_core.messages import BaseMessage, HumanMessage, AIMessageChunk, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from evan_agent.memory_review import ReviewCancelled

from evan_agent.auth import AuthContext
from evan_agent.conversation_db import ConversationDB, private_connection


@dataclass
class _Conversation:
    """内部记录，不直接交给调用方，防止外部修改历史或归属信息。"""

    owner: AuthContext
    messages: list[BaseMessage] = field(default_factory=list)
    lock: Any = field(default_factory=RLock)
    pending: dict | None = None
    busy: bool = False
    operation: dict | None = None


@dataclass(frozen=True)
class ConversationInfo:
    """会话列表只返回 ID 和消息数量，不暴露其他会话的内容。"""

    conversation_id: str
    message_count: int


class SessionBusyError(RuntimeError):
    """同一聊天正在执行或存在待审批事项。"""


class SessionStore:
    """会话仓库：支持内存测试与 SQLite 持久化，包含归属检查和单会话串行化。

    不同聊天可以并行；同一聊天通过独立锁串行执行，避免两轮同时覆盖历史。
    登录会话失效后，即便知道聊天 ID，也不能恢复访问。
    """

    def __init__(self, path=None) -> None:
        self.database = ConversationDB(path) if path is not None else None
        self._checkpoint_connection = None
        if self.database:
            from langgraph.checkpoint.sqlite import SqliteSaver
            checkpoint_path = self.database.path.with_name(self.database.path.stem + '.checkpoints.sqlite3')
            self._checkpoint_connection = private_connection(checkpoint_path)
            self.checkpointer = SqliteSaver(self._checkpoint_connection)
            self.checkpointer.setup()
        else:
            self.checkpointer = InMemorySaver()
        self._items: dict[str, _Conversation] = {}
        self._lock = RLock()
        if self.database:
            for key, owner, messages, pending, operation, was_busy in self.database.load():
                item = _Conversation(AuthContext(**owner), messages=messages, pending=pending, operation=operation)
                if was_busy and (operation or {}).get('status') == 'running':
                    # 已在执行中的写入可能已经提交，禁止启动时自动恢复／重放。
                    if operation and operation.get('thread_id'):
                        self.checkpointer.delete_thread(operation['thread_id'])
                    item.pending = None
                    if operation:
                        operation['status'] = 'interrupted'
                self._items[key] = item
                self._persist(key, item)

    def _persist(self, key, item):
        if self.database and self._items.get(key) is item:
            self.database.save(key, item)

    def close(self):
        """关闭数据库连接和独占锁，不删除未确认的审批。"""
        if self._checkpoint_connection is not None:
            self._checkpoint_connection.close()
            self._checkpoint_connection = None
        if self.database:
            self.database.close()

    @staticmethod
    def _check_identity(identity: AuthContext) -> None:
        if identity.revoked.is_set():
            raise PermissionError("当前登录会话已退出。")
        if identity.role == "owner":
            if not identity.is_owner:
                raise PermissionError("本人登录已过期，请重新登录。")
        elif identity.role != "guest" or identity.user_id is not None:
            raise PermissionError("无效的会话身份。")

    def _same_identity(self, left: AuthContext, right: AuthContext) -> bool:
        # 持久化模式：本人资料归属稳定用户 ID，重登可访问；访客仍按随机会话隔离。
        if self.database:
            if left.role == right.role == 'owner':
                return left.user_id == right.user_id == 'owner'
            return left.role == right.role == 'guest' and left.session_id == right.session_id
        # 内存模式维持旧的单次登录隔离，供测试和临时运行使用。
        return (left.session_id, left.user_id, left.role, left.expires_at) == (
            right.session_id, right.user_id, right.role, right.expires_at
        )

    def _get(self, identity: AuthContext, conversation_id: str) -> _Conversation:
        self._check_identity(identity)
        with self._lock:
            item = self._items.get(conversation_id)
            # 不区分“不存在”和“属于其他人”，避免泄露其他会话是否存在。
            if item is None or not self._same_identity(item.owner, identity):
                raise PermissionError("会话不存在或当前身份无权访问。")
            return item

    def create(self, identity: AuthContext) -> str:
        """为当前登录身份创建一个随机 ID 的空白聊天。"""
        self._check_identity(identity)
        conversation_id = uuid4().hex
        with self._lock:
            self._items[conversation_id] = _Conversation(owner=identity)
            self._persist(conversation_id, self._items[conversation_id])
        return conversation_id

    def list_for(self, identity: AuthContext) -> list[ConversationInfo]:
        """仅返回本次登录／访客身份创建的聊天，其他身份的数据不可见。"""
        self._check_identity(identity)
        with self._lock:
            return [
                ConversationInfo(key, len(item.messages))
                for key, item in self._items.items()
                if self._same_identity(item.owner, identity)
            ]

    def history(self, identity: AuthContext, conversation_id: str) -> list[BaseMessage]:
        """返回历史的深拷贝，调用方改动消息对象不会污染仓库。"""
        item = self._get(identity, conversation_id)
        with item.lock:
            self._get(identity, conversation_id)
            return deepcopy(item.messages)

    def clear(self, identity: AuthContext, conversation_id: str) -> None:
        """清空指定聊天内容，保留该聊天 ID。"""
        item = self._get(identity, conversation_id)
        with item.lock, self._lock:
            self._get(identity, conversation_id)
            if item.busy:
                raise SessionBusyError("该聊天正在执行，请稍后操作。")
            if item.pending:
                self.checkpointer.delete_thread(item.pending["thread_id"])
                item.pending = None
            item.messages.clear()
            item.operation = None
            self._persist(conversation_id, item)

    def discard_identity(self, identity: AuthContext) -> None:
        """删除该登录身份的全部聊天；身份过期时也允许执行清理。"""
        with self._lock:
            if self.database and identity.role == 'owner':
                return  # 注销撤销认证，保留本人聊天；后续访问仍需有效登录。
            if self.database:
                for key, item in self._items.items():
                    if self._same_identity(item.owner, identity):
                        self.database.delete(key)
            for item in self._items.values():
                if self._same_identity(item.owner, identity) and item.pending:
                    self.checkpointer.delete_thread(item.pending["thread_id"])
            self._items = {
                key: item for key, item in self._items.items()
                if not self._same_identity(item.owner, identity)
            }

    def pending_for(self, identity, conversation_id):
        """仅向所属登录会话返回待审批内容，支持页面刷新后恢复审批界面。"""
        with self._lock:
            item = self._get(identity, conversation_id)
            return deepcopy(item.pending["review"]) if item.pending else None

    def operation_for(self, identity, conversation_id):
        """断线后的只读核对入口，不重复执行模型或写入。"""
        with self._lock:
            item = self._get(identity, conversation_id)
            return {"running": item.busy, "operation": deepcopy(item.operation)}

    def save_review_draft(self, identity, conversation_id, approval_id, decisions):
        """暂存审批表单，不恢复 Agent，也不写个人资料；最终提交仍需确认。"""
        from evan_agent.auth import require_owner
        require_owner(identity)
        with self._lock:
            item = self._get(identity, conversation_id)
            if item.busy or not item.pending or item.pending["review"]["approval_id"] != approval_id:
                raise SessionBusyError("审批已失效、正在处理或已经完成。")
            item.pending["review"]["draft_decisions"] = deepcopy(decisions)
            self._persist(conversation_id, item)

    def cancel_pending(self, identity, conversation_id, approval_id):
        with self._lock:
            item = self._get(identity, conversation_id)
            if item.busy:
                raise SessionBusyError("该聊天正在执行。")
            if not item.pending or item.pending["review"]["approval_id"] != approval_id:
                raise SessionBusyError("审批已失效或已处理。")
            self.checkpointer.delete_thread(item.pending["thread_id"])
            item.pending = None
            item.operation = {"id": approval_id, "status": "cancelled", "writes": []}
            self._persist(conversation_id, item)

    def events(self, agent, identity, conversation_id, prompt=None, *, approval_id=None,
               decision=None, streaming=True):
        """共享的执行状态机；CLI 和 HTTP 都消费相同事件。

        到达审批点后返回 approval_required 并结束当前请求，保留 checkpoint。
        后续请求凭所属身份和审批 ID 恢复；重复审批、跨会话审批均拒绝。
        不跨 yield 持有线程锁，适配 HTTP 在线程池中迭代同步生成器。
        """
        with self._lock:
            item = self._get(identity, conversation_id)
            if item.busy:
                raise SessionBusyError("该聊天正在执行，请勿重复发送。")
            if approval_id is not None:
                from evan_agent.auth import require_owner
                require_owner(identity)
                if not item.pending or item.pending["review"]["approval_id"] != approval_id:
                    raise SessionBusyError("审批已失效或已处理。")
                thread_id = item.pending["thread_id"]
                known_messages = set(item.pending.get("seen_messages", []))
                value = Command(resume=decision)
            else:
                if item.pending:
                    raise SessionBusyError("请先确认或取消当前待审批事项。")
                if not isinstance(prompt, str) or not prompt.strip():
                    raise ValueError("消息不能为空。")
                thread_id = uuid4().hex
                known_messages = {message.id for message in item.messages}
                value = {"messages": [*deepcopy(item.messages), HumanMessage(content=prompt)]}
            item.pending = None
            item.busy = True
            item.operation = {"id": approval_id or thread_id, "thread_id": thread_id, "status": "running", "writes": []}
            self._persist(conversation_id, item)
        config = {"configurable": {"thread_id": thread_id}}
        context = replace(identity, conversation_id=conversation_id)
        paused = False
        def capture_receipts(state):
            with self._lock:
                for message in state.get("messages", []):
                    if isinstance(message, ToolMessage) and message.id not in known_messages:
                        artifact = message.artifact or {}
                        receipt = artifact.get("memory_write") if isinstance(artifact, dict) else None
                        if receipt:
                            item.operation["writes"].append(deepcopy(receipt))
                            known_messages.add(message.id)
                self._persist(conversation_id, item)
        try:
            result = {}
            active_message = object()
            if streaming:
                with closing(agent.stream(value, config=config, context=context,
                                          stream_mode=["messages", "values"])) as events:
                    for mode, payload in events:
                        self._get(identity, conversation_id)
                        if mode == "values":
                            result.update(payload)
                            capture_receipts(result)
                        elif mode == "messages":
                            chunk, metadata = payload
                            if metadata.get("langgraph_node") == "model" and isinstance(chunk, AIMessageChunk) and chunk.text:
                                key = chunk.id or metadata.get("langgraph_step")
                                if key != active_message:
                                    active_message = key
                                    yield {"type": "message_start"}
                                yield {"type": "delta", "text": chunk.text}
            else:
                result = agent.invoke(value, config=config, context=context)
                capture_receipts(result)
            with self._lock:
                self._get(identity, conversation_id)
                interrupts = result.get("__interrupt__")
                if interrupts:
                    if len(interrupts) != 1:
                        raise ReviewCancelled("审批中断结构异常，已停止本轮。")
                    # 暂停轮次尚未提交到聊天历史，单独提供本轮用户文本供页面刷新展示。
                    user_message = next((message.text for message in reversed(result.get("messages", []))
                                         if isinstance(message, HumanMessage)), "")
                    review = {"approval_id": uuid4().hex, "user_message": user_message,
                              **deepcopy(interrupts[0].value)}
                    item.pending = {"thread_id": thread_id, "review": review, "seen_messages": list(known_messages)}
                    item.operation["status"] = "awaiting_review"
                    paused = True
                    event = {"type": "approval_required", **review}
                else:
                    item.messages = deepcopy(result["messages"])
                    item.operation["status"] = "completed"
                    event = {"type": "done", "answer": item.messages[-1].text, "writes": deepcopy(item.operation["writes"])}
                self._persist(conversation_id, item)
            yield event
        finally:
            # 断流可能发生在工具落库之后、values 发出之前，从本轮 checkpoint 核对回执。
            if item.operation["status"] == "running":
                try:
                    snapshot = agent.get_state(config)
                    if isinstance(snapshot.values, dict):
                        capture_receipts(snapshot.values)
                except Exception:
                    pass  # 无 checkpoint 的测试替身等情况，保留已收到的回执。
                item.operation["status"] = "interrupted"
            with self._lock:
                item.busy = False
                if not paused or self._items.get(conversation_id) is not item:
                    self.checkpointer.delete_thread(thread_id)
                    item.pending = None
                self._persist(conversation_id, item)

    def invoke(self, agent: Any, identity: AuthContext, conversation_id: str, prompt: str,
               reviewer=None, on_text=None, on_message=None) -> str:
        """CLI 适配器：将共享事件转换为终端输出，并同步询问人工审批。"""
        next_args = {"prompt": prompt}
        while True:
            review = None
            with closing(self.events(agent, identity, conversation_id, streaming=on_text is not None,
                                     **next_args)) as events:
                for event in events:
                    if event["type"] == "message_start" and on_message:
                        on_message()
                    elif event["type"] == "delta" and on_text:
                        on_text(event["text"])
                    elif event["type"] == "approval_required":
                        review = event
                    elif event["type"] == "done":
                        return event["answer"]
            if review is None:
                raise RuntimeError("Agent 未返回最终结果。")
            try:
                self._get(identity, conversation_id)
                if reviewer is None:
                    raise ReviewCancelled("当前入口没有审批界面，未执行待审批写入。")
                decision = reviewer(deepcopy(review), identity)
                self._get(identity, conversation_id)
                next_args = {"approval_id": review["approval_id"], "decision": decision}
            except BaseException:
                # CLI 取消或身份过期时也要删除尚未执行的 checkpoint。
                with self._lock:
                    item = self._items.get(conversation_id)
                    if item and item.pending:
                        self.checkpointer.delete_thread(item.pending["thread_id"])
                        item.pending = None
                        self._persist(conversation_id, item)
                raise
