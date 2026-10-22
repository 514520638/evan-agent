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


@dataclass(frozen=True)
class ConversationInfo:
    """会话列表只返回 ID 和消息数量，不暴露其他会话的内容。"""

    conversation_id: str
    message_count: int


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
            item.messages.clear()

    def discard_identity(self, identity: AuthContext) -> None:
        """删除该登录身份的全部聊天；身份过期时也允许执行清理。"""
        with self._lock:
            self._items = {
                key: item for key, item in self._items.items()
                if not self._same_identity(item.owner, identity)
            }

    def invoke(self, agent: Any, identity: AuthContext, conversation_id: str, prompt: str, reviewer=None, on_text=None, on_message=None) -> str:
        """读取本会话历史、调用 Agent，并在成功后提交完整消息。

        模型异常时保持原历史不变。请求期间身份过期或会话被清除时，
        结果不写入仓库。流式模式会在每个片段显示前检查身份；已显示文字无法撤回。
        """
        item = self._get(identity, conversation_id)
        with item.lock:
            self._get(identity, conversation_id)
            # 深拷贝隔离模型处理期间可能发生的消息修改。
            messages = [*deepcopy(item.messages), HumanMessage(content=prompt)]
            # 每轮分配独立临时 thread，暂停与恢复共用该 ID。聊天历史仍由本仓库管理，
            # 避免 checkpoint 的历史与手动传入的消息叠加，计数也不会跨轮累计。
            thread_id = uuid4().hex
            config = {"configurable": {"thread_id": thread_id}}
            context = replace(identity, conversation_id=conversation_id)
            def run(value):
                if on_text is None:
                    return agent.invoke(value, config=config, context=context)
                result = {}
                active_message = object()
                # messages 用于即时显示，values 用于完整状态与 HITL 中断恢复。
                # 只显示 model 节点的文本，排除摘要模型、工具参数和 thinking 块。
                with closing(agent.stream(value, config=config, context=context,
                                          stream_mode=["messages", "values"])) as events:
                    for mode, payload in events:
                        self._get(identity, conversation_id)
                        if mode == "values":
                            result.update(payload)
                        elif mode == "messages":
                            chunk, metadata = payload
                            if metadata.get("langgraph_node") == "model" and isinstance(chunk, AIMessageChunk):
                                text = chunk.text
                                if text:
                                    message_key = chunk.id or metadata.get("langgraph_step")
                                    if message_key != active_message:
                                        if on_message is not None:
                                            on_message()
                                        active_message = message_key
                                    on_text(text)
                return result

            try:
                result = run({"messages": messages})
                while result.get("__interrupt__"):
                    self._get(identity, conversation_id)
                    if reviewer is None:
                        raise ReviewCancelled("写入需要人工确认；当前入口没有审批界面，未执行待审批写入。")
                    interrupts = result["__interrupt__"]
                    # 使用 batched HITL：一次中断中包含全部待审操作，按原顺序决策。
                    if len(interrupts) != 1:
                        raise ReviewCancelled("收到多个审批中断，已停止本轮以避免错误恢复。")
                    decision = reviewer(deepcopy(interrupts[0].value), identity)
                    self._get(identity, conversation_id)
                    result = run(Command(resume=decision))
                with self._lock:
                    self._get(identity, conversation_id)
                    item.messages = deepcopy(result["messages"])
                    return item.messages[-1].text
            finally:
                # 成功、取消、异常都删除本轮 checkpoint，避免留下可误恢复的审批状态。
                self.checkpointer.delete_thread(thread_id)
