"""LangChain Agent 的全部六类中间件钩子，包含同步和异步实现。

正常执行顺序：
before_agent → before_model → wrap_model_call → after_model
→（如有工具调用：wrap_tool_call → 再次进入模型阶段）→ after_agent。

每次 invoke / ainvoke 都有独立的 Agent 生命周期，不是整个命令行进程共用一次。
本模块仅输出执行阶段、消息数量、耗时和异常类型，不输出消息内容或密钥。
"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command


class LifecycleMiddleware(AgentMiddleware):
    """完整演示 Agent 中间件钩子，并原样传递状态和调用结果。

    invoke / stream 使用同步方法；ainvoke / astream 使用异步方法。
    同步和异步方法是两种执行入口，不会在一次调用中各执行一遍。
    节点钩子返回 None 表示不更新状态；如需更新，应返回字段更新字典。
    计时变量保存在调用栈中，多个请求不会共用计时状态。
    """

    @staticmethod
    def _log_state(hook: str, state: AgentState) -> None:
        """输出节点阶段与消息数量，便于观察调用顺序。"""
        print(f"[生命周期] {hook} | 消息数={len(state.get('messages', []))}")

    @staticmethod
    @contextmanager
    def _observe(hook: str) -> Iterator[None]:
        """给包裹式钩子统一记录进入、正常返回、异常和结束耗时。

        .before / .after / .error / .end 是本项目的日志标记，
        不是 LangChain 额外提供的钩子名。异常原样抛回调用方。
        """
        started = perf_counter()
        print(f"[生命周期] {hook}.before")
        try:
            yield
        except Exception as error:
            # 仅打印异常类型，具体 API 错误仍由 main 中的脱敏处理负责。
            print(f"[生命周期] {hook}.error | {type(error).__name__}")
            raise
        else:
            print(f"[生命周期] {hook}.after")
        finally:
            # 成功、异常或异步取消离开此作用域时均记录耗时。
            print(f"[生命周期] {hook}.end | 耗时={perf_counter() - started:.3f}s")

    # ---------- 同步入口：agent.invoke() / agent.stream() ----------

    def before_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        """每次 Agent 执行开始前触发一次，可用于输入校验或初始化状态。"""
        self._log_state("before_agent", state)
        return None

    def before_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        """每轮模型节点执行前触发，可用于整理历史或更新状态。

        工具执行后可能再次调用模型，因此一次 Agent 执行可触发多次。
        """
        self._log_state("before_model", state)
        return None

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """包裹模型调用，可用于修改请求、缓存或异常处理。

        request 包含模型、消息、状态与运行时上下文。
        本例调用 handler 一次，并原样返回结果，不额外重试模型请求。
        """
        with self._observe("wrap_model_call"):
            return handler(request)

    def after_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        """模型节点完成且响应已进入状态后触发，可检查响应或统计用量。

        此时模型可能请求调用工具，因此不一定已得到最终答案。
        """
        self._log_state("after_model", state)
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """包裹单个工具调用；多个工具调用会分别进入此钩子。

        request.tool_call 包含工具名和参数。这里不打印参数。
        工具可返回 ToolMessage 或状态更新 Command，必须原样传递。
        handler 正常返回也可能携带工具错误结果，不等同于业务成功。
        """
        with self._observe("wrap_tool_call"):
            return handler(request)

    def after_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        """Agent 正常到达结束阶段时触发一次，可用于结果整理。

        此钩子不是 finally；未处理的异常中断执行时不保证触发。
        必须执行的调用清理应放在包裹式钩子或调用方的 finally 中。
        """
        self._log_state("after_agent", state)
        return None

    # ---------- 异步入口：await agent.ainvoke() / agent.astream() ----------

    async def abefore_agent(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """before_agent 的异步版本，可在这里 await 异步初始化操作。"""
        self._log_state("abefore_agent", state)
        return None

    async def abefore_model(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """before_model 的异步版本，在每轮模型节点执行前触发。"""
        self._log_state("abefore_model", state)
        return None

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """wrap_model_call 的异步版本，必须 await 下游 handler。"""
        with self._observe("awrap_model_call"):
            return await handler(request)

    async def aafter_model(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """after_model 的异步版本，响应写入状态后触发。"""
        self._log_state("aafter_model", state)
        return None

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """wrap_tool_call 的异步版本，支持异步工具与状态更新结果。"""
        with self._observe("awrap_tool_call"):
            return await handler(request)

    async def aafter_agent(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """after_agent 的异步版本；同样不承担异常退出时的 finally 语义。"""
        self._log_state("aafter_agent", state)
        return None
