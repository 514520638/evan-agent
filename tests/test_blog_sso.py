"""统一登录与访客问题的 HTTP 边界，使用本地 Java 响应替身，不访问真实服务。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from evan_agent.api import create_app
from evan_agent.auth import AuthStore
from evan_agent.service import AgentService
from evan_agent.knowledge import KnowledgeStore
from evan_agent.sessions import SessionStore
from evan_agent.guest_questions import GuestQuestions


class BlogSSOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        auth = AuthStore(root / 'auth.sqlite3')
        self.service = AgentService(auth, KnowledgeStore(root / 'knowledge.sqlite3'), SessionStore())
        with patch('evan_agent.api.load_dotenv'), patch.dict('os.environ', {'AGENT_ALLOW_LEGACY_WEB_LOGIN': 'false'}):
            self.app = create_app(self.service)
        self.client = TestClient(self.app); self.addCleanup(self.client.close)
        self.revoked = False
        client_class = httpx.Client
        def respond(request):
            if request.url.path.endswith('/logout'):
                self.revoked = True
                return httpx.Response(200, json={'success': True})
            if self.revoked or 'valid-blog-cookie' not in request.headers.get('cookie', ''):
                return httpx.Response(401)
            return httpx.Response(200, json={'role': 'owner', 'expires_at': time.time() + 1000})
        self.bridge = patch('evan_agent.blog_identity.httpx.Client', side_effect=lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs))
        self.bridge.start(); self.addCleanup(self.bridge.stop)

    def test_blog_cookie_grants_owner_without_agent_password(self):
        self.client.cookies.set('evanlink_admin_token', 'valid-blog-cookie')
        result = self.client.get('/agent-api/auth/me')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['role'], 'owner')
        self.assertIn('evan_agent_session', result.headers['set-cookie'])
        self.client.cookies.delete('evanlink_admin_token')
        self.assertEqual(self.client.get('/agent-api/auth/me').status_code, 401)

    def test_agent_logout_revokes_blog_and_agent(self):
        self.client.cookies.set('evanlink_admin_token', 'valid-blog-cookie')
        self.client.get('/agent-api/auth/me')
        result = self.client.post('/agent-api/auth/logout', headers={'X-Agent-Request': '1'})
        self.assertEqual(result.status_code, 200); self.assertTrue(self.revoked)
        self.assertEqual(self.client.get('/agent-api/auth/me').status_code, 401)

    def test_guest_cannot_read_question_log_but_owner_can(self):
        questions = GuestQuestions(self.service.knowledge.path.parent / 'guest_questions.sqlite3')
        questions.add('你做过什么项目？')
        self.client.post('/agent-api/auth/guest', headers={'X-Agent-Request': '1'})
        self.assertEqual(self.client.get('/agent-api/guest-questions').status_code, 403)
        self.client.cookies.set('evanlink_admin_token', 'valid-blog-cookie')
        rows = self.client.get('/agent-api/guest-questions').json()['items']
        self.assertEqual(rows[0]['question'], '你做过什么项目？'); self.assertIn('created_at', rows[0])
        self.assertEqual(GuestQuestions(questions.path).list()['items'][0]['question'], rows[0]['question'])

    def test_expired_blog_cookie_can_return_to_guest(self):
        self.client.cookies.set('evanlink_admin_token', 'expired-cookie', domain='testserver.local', path='/')
        result = self.client.post('/agent-api/auth/guest', headers={'X-Agent-Request': '1'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['role'], 'guest')
        self.assertEqual(self.client.get('/agent-api/guest-questions').status_code, 403)

    def test_legacy_web_password_login_disabled_by_default(self):
        result = self.client.post('/agent-api/auth/login', json={'password': 'any-password'}, headers={'X-Agent-Request': '1'})
        self.assertEqual(result.status_code, 400)
