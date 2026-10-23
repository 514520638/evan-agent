"""按聊天 ID 存储短期上下文，并将每段对话绑定到创建它的登录会话。

这里仅使用进程内存；数据库持久化可在后续单独接入。
模型输入不承担权限校验，所有历史读取与写入都在此模块检查身份。
"""

from copy import deepcopy
from contextlib import closing
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any
from uuid import uuid4

from langchain_core.messages import BaseMessage, HumanMessage, AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from evan_agent.memory_review import ReviewCancelled

from evan_agent.auth import AuthContext


@dataclass
class _Conversation:
    """内部记录，不直接交给调用方，防止外部修改历史或归属信息。"""

    owner: AuthContext
    messages: list[BaseMessage] = field(default_factory=list)
    lock: Any = field(default_factory=RLock)
    pending: dict | None = None
    busy: bool = False


@dataclass(frozen=True)
class ConversationInfo:
    """会话列表只返回 ID 和消息数量，不暴露其他会话的内容。"""

    conversation_id: str
    message_count: int


class SessionBusyError(RuntimeError):
    """同一聊天正在执行或存在待审批事项。"""


class SessionStore:
    """内存会话仓库：独立历史、身份检查及单会话请求串行化。

    不同聊天可以并行；同一聊天通过独立锁串行执行，避免两轮同时覆盖历史。
    登录会话失效后，即便知道聊天 ID，也不能恢复访问。
    """

    def __init__(self) -> None:
        self.checkpointer = InMemorySaver()
        self._items: dict[str, _Conversation] = {}
        self._lock = RLock()

    @staticmethod
    def _check_identity(identity: AuthContext) -> None:
        if identity.revoked.is_set():
            raise PermissionError("当前登录会话已退出。")
        if identity.role == "owner":
            if not identity.is_owner:
                raise PermissionError("本人登录已过期，请重新登录。")
        elif identity.role != "guest" or identity.user_id is not None:
            raise PermissionError("无效的会话身份。")

    @staticmethod
    def _same_identity(left: AuthContext, right: AuthContext) -> bool:
        # 排除 conversation_id：它表示聊天，不是登录凭据。
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

    def discard_identity(self, identity: AuthContext) -> None:
        """删除该登录身份的全部聊天；身份过期时也允许执行清理。"""
        with self._lock:
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

    def cancel_pending(self, identity, conversation_id, approval_id):
        with self._lock:
            item = self._get(identity, conversation_id)
            if item.busy:
                raise SessionBusyError("该聊天正在执行。")
            if not item.pending or item.pending["review"]["approval_id"] != approval_id:
                raise SessionBusyError("审批已失效或已处理。")
            self.checkpointer.delete_thread(item.pending["thread_id"])
            item.pending = None

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
                value = Command(resume=decision)
            else:
                if item.pending:
                    raise SessionBusyError("请先确认或取消当前待审批事项。")
                if not isinstance(prompt, str) or not prompt.strip():
                    raise ValueError("消息不能为空。")
                thread_id = uuid4().hex
                value = {"messages": [*deepcopy(item.messages), HumanMessage(content=prompt)]}
            item.pending = None
            item.busy = True
        config = {"configurable": {"thread_id": thread_id}}
        context = replace(identity, conversation_id=conversation_id)
        paused = False
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
                    item.pending = {"thread_id": thread_id, "review": review}
                    paused = True
                    event = {"type": "approval_required", **review}
                else:
                    item.messages = deepcopy(result["messages"])
                    event = {"type": "done", "answer": item.messages[-1].text}
            yield event
        finally:
            with self._lock:
                item.busy = False
                if not paused or self._items.get(conversation_id) is not item:
                    self.checkpointer.delete_thread(thread_id)
                    item.pending = None

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
                raise
