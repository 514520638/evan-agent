"""HTTP 边界与跨请求审批验证；临时数据库、本地模型替身，无真实 API 调用。"""

from pathlib import Path
import tempfile
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain_core.messages import AIMessage

from evan_agent.api import create_app
from evan_agent.auth import AuthStore
from evan_agent.knowledge import KnowledgeStore
from evan_agent.knowledge_tools import build_knowledge_tools
from evan_agent.middleware import build_middleware, AuditWriter
from evan_agent.service import AgentService
from evan_agent.sessions import SessionStore
from test_streaming import StreamingToolModel

BASE = '/agent-api'
HEADERS = {'X-Agent-Request': '1'}


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        auth = AuthStore(root / 'auth.sqlite3')
        auth.initialize('test-password-only')
        knowledge = KnowledgeStore(root / 'knowledge.sqlite3')
        def factory(store, sessions):
            model = StreamingToolModel(responses=[
                AIMessage(content='', tool_calls=[{'name': 'save_personal_memory', 'args': {'content': '我在北京工作'}, 'id': 'memory', 'type': 'tool_call'}]),
                AIMessage(content='完成'),
            ])
            return create_agent(model=model, tools=build_knowledge_tools(store), context_schema=__import__('evan_agent.auth', fromlist=['AuthContext']).AuthContext,
                checkpointer=sessions.checkpointer,
                middleware=build_middleware(model, writer=AuditWriter(root / 'audit'), lifecycle_logs=False))
        self.service = AgentService(auth, knowledge, SessionStore(), factory)
        with patch('evan_agent.api.load_dotenv'), patch.dict('os.environ', {'AGENT_COOKIE_SECURE': 'false'}):
            self.app = create_app(self.service)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def login(self):
        response = self.client.post(BASE + '/auth/login', headers=HEADERS, json={'password': 'test-password-only'})
        self.assertEqual(response.status_code, 200)
        return response

    def chat(self):
        return self.client.post(BASE + '/conversations', headers=HEADERS).json()['conversation_id']

    def pending(self, chat):
        response = self.client.post(f'{BASE}/conversations/{chat}/messages', headers=HEADERS, json={'message': '记住我在北京工作'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/event-stream', response.headers['content-type'])
        data = events(response)
        self.assertEqual(data[-1]['type'], 'approval_required')
        return data[-1]

    def test_csrf_guest_cookie_and_cross_session_isolation(self):
        self.assertEqual(self.client.post(BASE + '/auth/guest').status_code, 403)
        self.assertEqual(self.client.post(BASE + '/auth/guest', headers={**HEADERS, 'Origin': 'https://evil.invalid'}).status_code, 403)
        r = self.client.post(BASE + '/auth/guest', headers=HEADERS)
        self.assertIn('HttpOnly', r.headers['set-cookie'])
        self.assertEqual(self.client.get(BASE + '/auth/me').json()['role'], 'guest')
        chat = self.chat()
        with TestClient(self.app) as other:
            other.post(BASE + '/auth/guest', headers=HEADERS)
            self.assertEqual(other.get(f'{BASE}/conversations/{chat}').status_code, 403)
        self.assertEqual(self.client.get(f'{BASE}/conversations/{chat}').status_code, 200)

    def test_login_rotation_logout_revocation(self):
        self.client.post(BASE + '/auth/guest', headers=HEADERS)
        old = self.client.cookies.get('evan_agent_session')
        self.login()
        self.assertNotEqual(old, self.client.cookies.get('evan_agent_session'))
        self.assertEqual(self.client.get(BASE + '/auth/me').json()['role'], 'owner')
        self.client.post(BASE + '/auth/logout', headers=HEADERS)
        self.assertEqual(self.client.get(BASE + '/auth/me').status_code, 401)
        self.client.cookies.set('evan_agent_session', old, path=BASE)
        self.assertEqual(self.client.get(BASE + '/auth/me').status_code, 401)

    def test_approval_pauses_without_write_then_edits_and_streams(self):
        self.login()
        chat = self.chat()
        pending = self.pending(chat)
        owner = self.service.sessions._items[chat].owner
        self.assertEqual(self.service.knowledge.list_entries(owner), [])
        self.assertEqual(self.client.get(f'{BASE}/conversations/{chat}').json()['pending']['approval_id'], pending['approval_id'])
        self.assertEqual(self.client.post(f'{BASE}/conversations/{chat}/messages', headers=HEADERS, json={'message': '新消息'}).status_code, 409)
        url = f"{BASE}/conversations/{chat}/approvals/{pending['approval_id']}"
        payload = {'decisions': [{'type': 'edit', 'changes': {'content': '我在北京工作，公司：示例公司', 'visibility': 'private', 'status': 'published'}}]}
        reply = self.client.post(url, headers=HEADERS, json=payload)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(events(reply)[-1]['type'], 'done')
        self.assertEqual(''.join(e['text'] for e in events(reply) if e['type'] == 'delta'), '完成')
        entry = self.service.knowledge.list_entries(owner)[0]
        self.assertIn('示例公司', entry.content)
        self.assertEqual(entry.visibility, 'private')
        self.assertEqual(self.client.post(url, headers=HEADERS, json=payload).status_code, 409)

    def test_approval_tampering_and_cancellation(self):
        self.login()
        chat = self.chat()
        pending = self.pending(chat)
        url = f"{BASE}/conversations/{chat}/approvals/{pending['approval_id']}"
        r = self.client.post(url, headers=HEADERS, json={'decisions': [{'type': 'edit', 'changes': {'owner_id': 'other'}}]})
        self.assertEqual(r.status_code, 400)
        with TestClient(self.app) as other:
            other.post(BASE + '/auth/guest', headers=HEADERS)
            self.assertEqual(other.post(url, headers=HEADERS, json={'decisions': [{'type': 'approve'}]}).status_code, 403)
        self.assertEqual(self.client.delete(url, headers=HEADERS).status_code, 200)
        self.assertIsNone(self.client.get(f'{BASE}/conversations/{chat}').json()['pending'])
        self.assertEqual(list(self.service.sessions.checkpointer.list(None)), [])
        self.assertEqual(self.service.knowledge.list_entries(self.service.sessions._items[chat].owner), [])

    def test_reject_does_not_write(self):
        self.login()
        chat = self.chat()
        pending = self.pending(chat)
        r = self.client.post(f"{BASE}/conversations/{chat}/approvals/{pending['approval_id']}", headers=HEADERS,
                             json={'decisions': [{'type': 'reject'}]})
        self.assertEqual(events(r)[-1]['type'], 'done')
        self.assertEqual(self.service.knowledge.list_entries(self.service.sessions._items[chat].owner), [])

    def test_logout_clears_pending_and_blocks_old_cookie(self):
        self.login()
        chat = self.chat()
        pending = self.pending(chat)
        owner = self.service.sessions._items[chat].owner
        token = self.client.cookies.get('evan_agent_session')
        self.client.post(BASE + '/auth/logout', headers=HEADERS)
        self.assertFalse(owner.is_owner)
        self.client.cookies.set('evan_agent_session', token, path=BASE)
        r = self.client.post(f"{BASE}/conversations/{chat}/approvals/{pending['approval_id']}", headers=HEADERS,
                             json={'decisions': [{'type': 'approve'}]})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(list(self.service.sessions.checkpointer.list(None)), [])


if __name__ == '__main__':
    unittest.main()
