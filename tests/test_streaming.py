"""验证实时文本片段、完整历史提交和人工审批后继续输出。"""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel, FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk

from evan_agent.auth import AuthContext
from evan_agent.sessions import SessionStore
from evan_agent.knowledge import KnowledgeStore
from evan_agent.knowledge_tools import build_knowledge_tools


class StreamingToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        response = self._generate(messages, stop=stop, run_manager=run_manager).generations[0].message
        if response.tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content='', tool_call_chunks=[
                {'name': call['name'], 'args': json.dumps(call['args']), 'id': call['id'], 'index': i}
                for i, call in enumerate(response.tool_calls)
            ]))
        else:
            yield ChatGenerationChunk(message=AIMessageChunk(content=[{'type': 'thinking', 'thinking': '不要显示内部内容', 'index': 0}]))
            for char in response.text:
                yield ChatGenerationChunk(message=AIMessageChunk(content=[{'type': 'text', 'text': char, 'index': 1}]))


class StreamingTests(unittest.TestCase):
    def test_tokens_arrive_before_history_commit(self):
        sessions, identity = SessionStore(), AuthContext.guest()
        chat = sessions.create(identity)
        agent = create_agent(model=FakeListChatModel(responses=['你好世界']), tools=[],
                             context_schema=AuthContext, checkpointer=sessions.checkpointer)
        pieces = []
        def collect(text):
            self.assertEqual(sessions.history(identity, chat), [])
            pieces.append(text)
        answer = sessions.invoke(agent, identity, chat, '你好', on_text=collect)
        self.assertEqual(''.join(pieces), '你好世界')
        self.assertGreater(len(pieces), 1)
        self.assertEqual(answer, '你好世界')
        self.assertEqual(len(sessions.history(identity, chat)), 2)

    def test_partial_failure_does_not_commit_history(self):
        sessions, identity = SessionStore(), AuthContext.guest()
        chat = sessions.create(identity)
        def broken(*args, **kwargs):
            yield ('messages', (AIMessageChunk(content='部分文本'), {'langgraph_node': 'model'}))
            raise RuntimeError('模拟流中断')
        pieces = []
        with self.assertRaises(RuntimeError):
            sessions.invoke(Mock(stream=broken), identity, chat, '问题', on_text=pieces.append)
        self.assertEqual(pieces, ['部分文本'])
        self.assertEqual(sessions.history(identity, chat), [])

    def test_stream_resumes_after_hitl_and_hides_thinking(self):
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory) / 'knowledge.sqlite3')
            sessions = SessionStore()
            owner = AuthContext('login', 'owner', 'owner', time.monotonic() + 60)
            chat = sessions.create(owner)
            model = StreamingToolModel(responses=[
                AIMessage(content='', tool_calls=[{'name': 'save_personal_memory', 'args': {'content': '审批前不写入'}, 'id': 'write', 'type': 'tool_call'}]),
                AIMessage(content='已经保存'),
            ])
            agent = create_agent(model=model, tools=build_knowledge_tools(store), context_schema=AuthContext,
                checkpointer=sessions.checkpointer,
                middleware=[HumanInTheLoopMiddleware(interrupt_on={'save_personal_memory': True}, interrupt_mode='batched')])
            def reviewer(payload, identity):
                self.assertEqual(store.list_entries(owner), [])
                return {'decisions': [{'type': 'approve'}]}
            pieces = []
            answer = sessions.invoke(agent, owner, chat, '记住', reviewer=reviewer, on_text=pieces.append)
            self.assertEqual(''.join(pieces), '已经保存')
            self.assertEqual(answer, '已经保存')
            self.assertEqual(len(store.list_entries(owner)), 1)
            self.assertEqual(list(sessions.checkpointer.list(None)), [])


if __name__ == '__main__':
    unittest.main()
