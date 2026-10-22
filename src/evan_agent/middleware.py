"""Agent 中间件装配：短期摘要、元数据审计和每轮调用限制。"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any
from uuid import uuid4
import warnings

from langchain.agents.middleware import (
    AgentMiddleware, AgentState, ModelCallLimitMiddleware,
    SummarizationMiddleware, ToolCallLimitMiddleware, HumanInTheLoopMiddleware,
)
from typing_extensions import NotRequired

from evan_agent.lifecycle import LifecycleMiddleware
from evan_agent.persona import PersonaMiddleware, configured_model_name

DEFAULT_AUDIT_DIR = Path(__file__).resolve().parents[2] / ".local" / "audit"


@dataclass(frozen=True)
class MiddlewareSettings:
    """所有限额均为正数，模型与工具限制作用于单次 invoke。"""

    summary_tokens: int = 6000
    summary_keep_messages: int = 10
    model_run_limit: int = 6
    tool_run_limit: int = 8

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("中间件阈值与调用次数必须为正整数。")

    @classmethod
    def from_env(cls):
        try:
            return cls(
                summary_tokens=int(os.getenv("SUMMARY_TRIGGER_TOKENS", "6000")),
                summary_keep_messages=int(os.getenv("SUMMARY_KEEP_MESSAGES", "10")),
                model_run_limit=int(os.getenv("MODEL_RUN_LIMIT", "6")),
                tool_run_limit=int(os.getenv("TOOL_RUN_LIMIT", "8")),
            )
        except ValueError:
            raise ValueError("请检查 SUMMARY_TRIGGER_TOKENS、SUMMARY_KEEP_MESSAGES、MODEL_RUN_LIMIT、TOOL_RUN_LIMIT，均须为正整数。") from None


class AuditState(AgentState):
    """每轮独立的审计关联信息，不放在中间件实例上共享。"""

    audit_trace_id: NotRequired[str]
    audit_started: NotRequired[float]


class AuditWriter:
    """按 UTC 日期写 JSONL；不保存正文、工具参数、结果或异常详情。"""

    def __init__(self, directory: Path = DEFAULT_AUDIT_DIR):
        self.directory = directory
        self._lock = Lock()
        self._warned = False

    def write(self, event: str, state: dict, context: Any, **metadata):
        now = datetime.now(timezone.utc)
        record = {
            "timestamp": now.isoformat(), "event": event,
            "trace_id": state.get("audit_trace_id"),
            "conversation_id": getattr(context, "conversation_id", None),
            "role": getattr(context, "role", "unknown"),
            **metadata,
        }
        # 只接收本模块构造的元数据；不序列化 state、context 或 request 本身。
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = self.directory / f"{now.date().isoformat()}.jsonl"
                fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as stream:
                    stream.write(line)
            except OSError:
                # 审计磁盘故障不替代模型结果；提示一次，避免日志故障刷屏。
                if not self._warned:
                    warnings.warn("审计日志写入失败，请检查 .local/audit 的权限或磁盘空间。", RuntimeWarning)
                    self._warned = True


def usage_of(response) -> dict[str, int] | None:
    """仅聚合服务端提供的用量；缺失时标记未知，不把估算冒充账单。"""
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    known = False
    for message in response.result:
        usage = getattr(message, "usage_metadata", None)
        if usage:
            known = True
            for key in totals:
                totals[key] += usage.get(key, 0)
    return totals if known else None


class AuditMiddleware(AgentMiddleware):
    """记录每轮开始／正常结束和模型、工具调用；异常只记录类型。

    after_agent 不是 finally：执行中断时可能只有 error 事件，没有 agent.end。
    当前调用方仅保存 messages，因此审计状态和 run 计数不会跨轮累计。
    """

    state_schema = AuditState

    def __init__(self, writer: AuditWriter):
        self.writer = writer

    def before_agent(self, state, runtime):
        update = {"audit_trace_id": uuid4().hex, "audit_started": perf_counter()}
        self.writer.write("agent.start", update, runtime.context, message_count=len(state["messages"]))
        return update

    async def abefore_agent(self, state, runtime):
        return self.before_agent(state, runtime)

    def after_agent(self, state, runtime):
        self.writer.write(
            "agent.end", state, runtime.context,
            duration_ms=round((perf_counter() - state["audit_started"]) * 1000),
            model_calls=state.get("run_model_call_count", 0),
            tool_calls=state.get("run_tool_call_count", {}),
        )
        return None

    async def aafter_agent(self, state, runtime):
        return self.after_agent(state, runtime)

    def _event(self, kind, request, started, response=None, error=None):
        metadata = {"duration_ms": round((perf_counter() - started) * 1000)}
        if kind == "model":
            metadata["requested_model"] = configured_model_name(request.model)
            reported = []
            if response is not None:
                for message in response.result:
                    info = getattr(message, "response_metadata", {})
                    name = info.get("model") or info.get("model_name")
                    if isinstance(name, str) and name not in reported:
                        reported.append(name)
            metadata["reported_models"] = reported
        if error is not None:
            metadata["error_type"] = type(error).__name__
        elif kind == "model":
            metadata["usage"] = usage_of(response)
        elif kind == "tool":
            metadata["status"] = getattr(response, "status", "command")
        self.writer.write(kind + (".error" if error is not None else ".end"),
                          request.state, request.runtime.context, **metadata)

    def wrap_model_call(self, request, handler):
        started = perf_counter()
        try:
            response = handler(request)
        except Exception as error:
            self._event("model", request, started, error=error)
            raise
        self._event("model", request, started, response=response)
        return response

    async def awrap_model_call(self, request, handler):
        started = perf_counter()
        try:
            response = await handler(request)
        except Exception as error:
            self._event("model", request, started, error=error)
            raise
        self._event("model", request, started, response=response)
        return response

    def wrap_tool_call(self, request, handler):
        started = perf_counter()
        try:
            response = handler(request)
        except Exception as error:
            self._event("tool", request, started, error=error)
            raise
        self._event("tool", request, started, response=response)
        return response

    async def awrap_tool_call(self, request, handler):
        started = perf_counter()
        try:
            response = await handler(request)
        except Exception as error:
            self._event("tool", request, started, error=error)
            raise
        self._event("tool", request, started, response=response)
        return response


class AuditedSummarizationMiddleware(SummarizationMiddleware):
    """沿用官方摘要实现，为额外的摘要请求添加独立审计事件。

    摘要请求不经过 Agent 的 wrap_model_call，因此不能计入模型节点调用量。
    官方摘要接口仅返回摘要文本，这里不推测摘要的 Token 用量。
    """

    def __init__(self, model, writer: AuditWriter, settings: MiddlewareSettings):
        super().__init__(model=model, trigger=("tokens", settings.summary_tokens),
                         keep=("messages", settings.summary_keep_messages))
        self.writer = writer

    def _record(self, state, runtime, started, update=None, error=None):
        if update is not None or error is not None:
            metadata = {"duration_ms": round((perf_counter() - started) * 1000), "usage": None}
            if error is not None:
                metadata["error_type"] = type(error).__name__
            else:
                metadata["messages_before"] = len(state["messages"])
                # 官方返回列表的第一项是清空历史的 RemoveMessage 指令。
                metadata["messages_after"] = len(update["messages"]) - 1
            self.writer.write("summary.error" if error is not None else "summary.end",
                              state, runtime.context, **metadata)

    def before_model(self, state, runtime):
        started = perf_counter()
        try:
            update = super().before_model(state, runtime)
        except Exception as error:
            self._record(state, runtime, started, error=error)
            raise
        self._record(state, runtime, started, update=update)
        return update

    async def abefore_model(self, state, runtime):
        started = perf_counter()
        try:
            update = await super().abefore_model(state, runtime)
        except Exception as error:
            self._record(state, runtime, started, error=error)
            raise
        self._record(state, runtime, started, update=update)
        return update


def build_middleware(model, settings: MiddlewareSettings | None = None,
                     writer: AuditWriter | None = None, *, lifecycle_logs: bool = True) -> list[AgentMiddleware]:
    """统一装配顺序，测试可传入临时日志目录及小阈值。"""
    settings = settings or MiddlewareSettings.from_env()
    writer = writer or AuditWriter()
    return [
        AuditMiddleware(writer),
        *([LifecycleMiddleware()] if lifecycle_logs else []),
        PersonaMiddleware(),
        # before_model 正序：先检查上限，避免达到上限后继续付费生成摘要。
        ModelCallLimitMiddleware(run_limit=settings.model_run_limit, exit_behavior="end"),
        AuditedSummarizationMiddleware(model, writer, settings),
        # after_model 逆序执行：末尾的工具限额先检查，再暂停待写入记忆。
        HumanInTheLoopMiddleware(
            interrupt_on={
                "save_personal_memory": {"allowed_decisions": ["approve", "edit", "reject"]},
                "update_personal_memory": {"allowed_decisions": ["approve", "edit", "reject"]},
            },
            interrupt_mode="batched",
            description_prefix="长期记忆写入前，请补充并确认内容、公开范围和发布状态",
        ),
        # after_model 逆序：先执行工具上限检查，超限结束本轮且不执行待定工具。
        ToolCallLimitMiddleware(run_limit=settings.tool_run_limit, exit_behavior="end"),
    ]
