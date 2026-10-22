"""离线验证摘要、审计、调用限额及与会话隔离的配合。"""

from dataclasses import replace
import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from evan_agent.auth import AuthContext
from evan_agent.middleware import AuditWriter, MiddlewareSettings, build_middleware
from evan_agent.sessions import SessionStore


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class MiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.writer = AuditWriter(self.directory)
        self.context = replace(AuthContext.guest(), conversation_id="test-chat")
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def events(self):
        return [json.loads(line) for path in self.directory.glob('*.jsonl') for line in path.read_text().splitlines()]

    def agent(self, model, settings=None, tools=None):
        return create_agent(model=model, tools=tools or [], context_schema=AuthContext,
                            middleware=build_middleware(model, settings or MiddlewareSettings(), self.writer))

    def test_audit_metadata_usage_and_trace_without_content(self):
        model = FakeMessagesListChatModel(responses=[AIMessage(content="PRIVATE_RESPONSE", usage_metadata={
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
        })])
        self.agent(model).invoke({"messages": [HumanMessage(content="PRIVATE_PROMPT")]}, context=self.context)
        events = self.events()
        self.assertEqual([e['event'] for e in events], ['agent.start', 'model.end', 'agent.end'])
        self.assertEqual(len({e['trace_id'] for e in events}), 1)
        self.assertEqual(events[1]['usage']['total_tokens'], 15)
        self.assertEqual(events[-1]['model_calls'], 1)
        self.assertTrue(all(e['conversation_id'] == 'test-chat' for e in events))
        self.assertNotIn('PRIVATE_', json.dumps(events))

    def test_summary_is_saved_in_one_chat_only(self):
        model = FakeMessagesListChatModel(responses=[AIMessage(content="压缩后的短文本")])
        agent = self.agent(model, MiddlewareSettings(summary_tokens=40, summary_keep_messages=2))
        store, identity = SessionStore(), AuthContext.guest()
        a, b = store.create(identity), store.create(identity)
        for _ in range(3):
            store.invoke(agent, identity, a, "本聊天的旧信息。" * 80)
        store.invoke(agent, identity, b, "你好")
        summaries = [e for e in self.events() if e['event'] == 'summary.end']
        self.assertTrue(summaries)
        self.assertTrue(all(e['conversation_id'] == a for e in summaries))
        self.assertLess(len(store.history(identity, a)), 6)
        self.assertEqual(len(store.history(identity, b)), 2)

    def test_model_limit_stops_loop_and_resets_next_invocation(self):
        executions = []
        @tool
        def ping() -> str:
            """返回本地固定结果。"""
            executions.append(1)
            return "pong"
        model = ToolModel(responses=[AIMessage(content="", tool_calls=[{
            "name": "ping", "args": {}, "id": "call-1", "type": "tool_call",
        }])])
        agent = self.agent(model, MiddlewareSettings(model_run_limit=1), [ping])
        for _ in range(2):
            result = agent.invoke({"messages": [HumanMessage(content="调用工具")]}, context=self.context)
            self.assertTrue(result['messages'][-1].text)
        self.assertEqual(len(executions), 2)
        self.assertEqual(len([e for e in self.events() if e['event'] == 'model.end']), 2)
        self.assertEqual([e['model_calls'] for e in self.events() if e['event'] == 'agent.end'], [1, 1])

    def test_tool_limit_blocks_excess_batch(self):
        executions = []
        @tool
        def ping() -> str:
            """返回本地固定结果。"""
            executions.append(1)
            return "pong"
        model = ToolModel(responses=[AIMessage(content="", tool_calls=[
            {"name": "ping", "args": {}, "id": f"call-{i}", "type": "tool_call"} for i in range(2)
        ])])
        result = self.agent(model, MiddlewareSettings(tool_run_limit=1), [ping]).invoke(
            {"messages": [HumanMessage(content="调用两次工具")]}, context=self.context)
        self.assertEqual(executions, [])
        self.assertTrue(result['messages'][-1].text)

    def test_async_agent_audit(self):
        model = FakeMessagesListChatModel(responses=[AIMessage(content="回复")])
        asyncio.run(self.agent(model).ainvoke({"messages": [HumanMessage(content="问题")]}, context=self.context))
        self.assertEqual([e['event'] for e in self.events()], ['agent.start', 'model.end', 'agent.end'])

    def test_invalid_threshold_rejected(self):
        with self.assertRaises(ValueError):
            MiddlewareSettings(model_run_limit=0)


if __name__ == '__main__':
    unittest.main()
