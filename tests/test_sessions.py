"""会话隔离检查：不读取真实凭据，也不调用远程模型。"""

from dataclasses import replace
from threading import Event
from concurrent.futures import ThreadPoolExecutor
import time
import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage

from evan_agent.auth import AuthContext
from evan_agent.sessions import SessionStore


def echo_agent():
    return Mock(invoke=Mock(side_effect=lambda value, **kwargs: {
        "messages": [*value["messages"], AIMessage(content="模拟回答")]
    }))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.store = SessionStore()
        self.identity = AuthContext.guest()
        self.a = self.store.create(self.identity)
        self.b = self.store.create(self.identity)
        self.agent = echo_agent()

    def test_independent_history_and_runtime_context(self):
        self.store.invoke(self.agent, self.identity, self.a, "A 的秘密")
        self.store.invoke(self.agent, self.identity, self.b, "B 的问题")
        self.store.invoke(self.agent, self.identity, self.a, "继续 A")
        calls = self.agent.invoke.call_args_list
        self.assertEqual([len(c.args[0]['messages']) for c in calls], [1, 1, 3])
        self.assertEqual([c.kwargs['context'].conversation_id for c in calls], [self.a, self.b, self.a])
        self.assertNotIn("A 的秘密", str(calls[1].args[0]))
        self.assertIsNone(self.identity.conversation_id)

    def test_foreign_guest_cannot_read_clear_or_invoke(self):
        stranger = AuthContext.guest()
        self.assertEqual(self.store.list_for(stranger), [])
        for operation in (
            lambda: self.store.history(stranger, self.a),
            lambda: self.store.clear(stranger, self.a),
            lambda: self.store.invoke(self.agent, stranger, self.a, "窃取历史"),
        ):
            with self.assertRaises(PermissionError):
                operation()
        self.agent.invoke.assert_not_called()

    def test_owner_login_instances_are_isolated(self):
        first = AuthContext("login-a", "owner", "owner", time.monotonic() + 60)
        second = replace(first, session_id="login-b")
        chat = self.store.create(first)
        with self.assertRaises(PermissionError):
            self.store.history(second, chat)
        with self.assertRaises(PermissionError):
            self.store.history(replace(first, expires_at=0), chat)

    def test_failed_model_does_not_commit_or_mutate_history(self):
        self.store.invoke(self.agent, self.identity, self.a, "已完成输入")
        def fail(value, **kwargs):
            value['messages'][0].content = '被模型改动'
            raise RuntimeError("模拟失败")
        with self.assertRaises(RuntimeError):
            self.store.invoke(Mock(invoke=fail), self.identity, self.a, "失败输入")
        history = self.store.history(self.identity, self.a)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].content, '已完成输入')
        history[0].content = '被调用方改动'
        self.assertEqual(self.store.history(self.identity, self.a)[0].content, '已完成输入')

    def test_clear_only_current_and_logout_removes_all(self):
        for chat in (self.a, self.b):
            self.store.invoke(self.agent, self.identity, chat, "内容")
        self.store.clear(self.identity, self.a)
        self.assertEqual(self.store.history(self.identity, self.a), [])
        self.assertEqual(len(self.store.history(self.identity, self.b)), 2)
        self.store.discard_identity(self.identity)
        self.assertEqual(self.store.list_for(self.identity), [])
        with self.assertRaises(PermissionError):
            self.store.history(self.identity, self.b)

    def test_expiry_during_request_does_not_return_private_result(self):
        owner = AuthContext("login", "owner", "owner", time.monotonic() + 60)
        chat = self.store.create(owner)
        with patch('evan_agent.auth.time.monotonic', return_value=owner.expires_at-1) as clock:
            def expire(value, **kwargs):
                clock.return_value = owner.expires_at + 1
                return {'messages': [AIMessage(content='私人回复')]}
            agent = Mock(invoke=expire)
            with self.assertRaises(PermissionError):
                self.store.invoke(agent, owner, chat, "问题")
        self.assertEqual(self.store.history(owner, chat), [])

    def test_logout_during_request_invalidates_pending_result(self):
        entered, release = Event(), Event()
        def delayed(value, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("检查超时")
            return {"messages": [AIMessage(content="旧会话回复")]}
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(self.store.invoke, Mock(invoke=delayed), self.identity, self.a, "问题")
            try:
                self.assertTrue(entered.wait(3))
                self.store.discard_identity(self.identity)
            finally:
                release.set()
            with self.assertRaises(PermissionError):
                pending.result(timeout=3)


if __name__ == '__main__':
    unittest.main()
