"""真实 HITL 中断／恢复链路的离线检查，不访问真实数据库或模型 API。"""

from contextlib import redirect_stdout
from pathlib import Path
import io
import tempfile
import time
import unittest
from unittest.mock import patch

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from evan_agent.auth import AuthContext
from evan_agent.knowledge import KnowledgeStore
from evan_agent.knowledge_tools import build_knowledge_tools
from evan_agent.memory_review import MemoryReviewer, ReviewCancelled
from evan_agent.middleware import build_middleware, AuditWriter
from evan_agent.sessions import SessionStore


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class MemoryReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = KnowledgeStore(Path(self.temp.name) / 'knowledge.sqlite3')
        self.owner = AuthContext('login', 'owner', 'owner', time.monotonic() + 60)
        self.sessions = SessionStore()
        self.chat = self.sessions.create(self.owner)
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def agent(self, name='save_personal_memory', args=None):
        model = ToolModel(responses=[AIMessage(content='', tool_calls=[{
            'name': name, 'args': args or {'content': '我在北京工作'}, 'id': 'write-1', 'type': 'tool_call',
        }]), AIMessage(content='已处理')])
        return create_agent(model=model, tools=build_knowledge_tools(self.store), context_schema=AuthContext,
                            middleware=build_middleware(model, writer=AuditWriter(Path(self.temp.name) / 'audit')),
                            checkpointer=self.sessions.checkpointer)

    def test_write_pauses_then_saves_edited_information(self):
        def reviewer(payload, identity):
            self.assertEqual(self.store.list_entries(identity), [])
            self.assertEqual(payload['action_requests'][0]['name'], 'save_personal_memory')
            return MemoryReviewer(self.store)(payload, identity)
        answers = ['', '示例公司', '开发工程师', '软件', '', '', '', '', '确认写入']
        with patch('builtins.input', side_effect=answers):
            self.sessions.invoke(self.agent(), self.owner, self.chat, '记住我在北京工作', reviewer=reviewer)
        entry = self.store.list_entries(self.owner)[0]
        self.assertIn('公司：示例公司', entry.content)
        self.assertIn('职位：开发工程师', entry.content)
        self.assertEqual((entry.visibility, entry.status), ('public', 'published'))
        self.assertEqual(len(self.store.list_entries(AuthContext.guest())), 1)
        self.assertEqual(list(self.sessions.checkpointer.list(None)), [])

    def test_cancel_does_not_write_or_commit_history(self):
        with patch('builtins.input', side_effect=['/cancel']):
            with self.assertRaises(ReviewCancelled):
                self.sessions.invoke(self.agent(), self.owner, self.chat, '记住', reviewer=MemoryReviewer(self.store))
        self.assertEqual(self.store.list_entries(self.owner), [])
        self.assertEqual(self.sessions.history(self.owner, self.chat), [])
        self.assertEqual(list(self.sessions.checkpointer.list(None)), [])

    def test_missing_reviewer_fails_closed(self):
        with self.assertRaises(ReviewCancelled):
            self.sessions.invoke(self.agent(), self.owner, self.chat, '记住')
        self.assertEqual(self.store.list_entries(self.owner), [])

    def test_update_waits_for_confirmation_and_keeps_original_on_cancel(self):
        original = self.store.create(self.owner, content='我在上海工作', status='published')
        agent = self.agent('update_personal_memory', {'entry_id': original.id, 'content': '我在北京工作'})
        with patch('builtins.input', side_effect=['/cancel']):
            with self.assertRaises(ReviewCancelled):
                self.sessions.invoke(agent, self.owner, self.chat, '更新工作', reviewer=MemoryReviewer(self.store))
        self.assertEqual(self.store.get(self.owner, original.id).content, '我在上海工作')

    def test_expired_login_during_review_prevents_resume(self):
        def reviewer(payload, identity):
            # 在恢复执行前使认证时钟超时；不接触真正的账号数据库。
            clock = patch('evan_agent.auth.time.monotonic', return_value=identity.expires_at + 1)
            clock.start()
            self.addCleanup(clock.stop)
            return {'decisions': [{'type': 'approve'}]}
        with self.assertRaises(PermissionError):
            self.sessions.invoke(self.agent(), self.owner, self.chat, '记住', reviewer=reviewer)
        self.assertEqual(self.store.list_entries(AuthContext.guest()), [])
        self.assertEqual(list(self.sessions.checkpointer.list(None)), [])


if __name__ == '__main__':
    unittest.main()
