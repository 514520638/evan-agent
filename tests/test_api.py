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

    def test_delete_chat_removes_pending_checkpoint_and_denies_cross_identity(self):
        self.login()
        chat = self.chat()
        self.pending(chat)
        item = self.service.sessions._items[chat]
        thread = item.pending['thread_id']
        self.assertTrue(list(self.service.sessions.checkpointer.list({'configurable': {'thread_id': thread}})))
        self.assertEqual(self.client.delete(BASE+'/conversations/'+chat, headers=HEADERS).status_code, 200)
        self.assertEqual(list(self.service.sessions.checkpointer.list({'configurable': {'thread_id': thread}})), [])
        self.assertEqual(self.client.get(BASE+'/conversations').json()['items'], [])
        other = self.chat()
        self.client.post(BASE+'/auth/logout', headers=HEADERS)
        self.client.post(BASE+'/auth/guest', headers=HEADERS)
        self.assertEqual(self.client.delete(BASE+'/conversations/'+other, headers=HEADERS).status_code, 403)

    def test_profile_management_owner_only_and_visibility(self):
        """管理入口必须认证，修改公开范围后访客立即失去读取权限。"""
        from evan_agent.auth import AuthContext
        self.assertEqual(self.client.get(BASE + '/profiles').status_code, 401)
        self.client.post(BASE + '/auth/guest', headers=HEADERS)
        body = {'content': '测试教育经历', 'category': '教育', 'visibility': 'public', 'status': 'published'}
        self.assertEqual(self.client.get(BASE + '/profiles').status_code, 403)
        self.assertEqual(self.client.post(BASE + '/profiles', headers=HEADERS, json=body).status_code, 403)
        self.login()
        created = self.client.post(BASE + '/profiles', headers=HEADERS, json=body)
        self.assertEqual(created.status_code, 201)
        entry_id = created.json()['id']
        self.assertEqual(self.client.get(BASE + '/profiles').json()['items'][0]['id'], entry_id)
        self.assertEqual(len(self.service.knowledge.list_entries(AuthContext.guest())), 1)
        changed = self.client.patch(BASE + '/profiles/' + entry_id, headers=HEADERS, json={**body, 'visibility': 'private'})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.service.knowledge.list_entries(AuthContext.guest()), [])
        self.assertEqual(self.client.post(BASE + '/profiles', headers=HEADERS, json={**body, 'owner_id': 'other'}).status_code, 422)
        self.assertEqual(self.client.post(BASE + '/profiles', headers=HEADERS, json={**body, 'content': '   '}).status_code, 422)
        self.assertEqual(self.client.delete(BASE + '/profiles/' + entry_id, headers=HEADERS).status_code, 200)
        self.assertEqual(self.client.patch(BASE + '/profiles/' + entry_id, headers=HEADERS, json=body).status_code, 404)

    def chat(self):
        return self.client.post(BASE + '/conversations', headers=HEADERS).json()['conversation_id']

    def test_image_upload_isolation_expiry_and_clear(self):
        from PIL import Image
        from io import BytesIO
        from unittest.mock import patch
        self.client.post(BASE+'/auth/guest', headers=HEADERS)
        chat = self.chat()
        other = self.chat()
        encoded = BytesIO()
        Image.new('RGB', (20, 20), 'blue').save(encoded, format='PNG')
        response = self.client.post(f'{BASE}/conversations/{chat}/images', headers=HEADERS, content=encoded.getvalue())
        self.assertEqual(response.status_code, 200)
        url, key = response.json()['url'], response.json()['id']
        self.assertEqual(self.client.get(url).headers['content-type'], 'image/png')
        self.assertEqual(self.client.get(url.replace(chat, other)).status_code, 410)
        self.assertEqual(self.client.post(f'{BASE}/conversations/{chat}/images', headers=HEADERS, content=b'not image').status_code, 400)
        self.assertEqual(self.client.post(f'{BASE}/conversations/{chat}/images', headers=HEADERS, content=b'x'*(2*1024*1024+1)).status_code, 413)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.service.images.path)) as db, db:
            db.execute('UPDATE images SET expires=0 WHERE id=?', (key,))
        self.assertEqual(self.client.get(url).status_code, 410)
        self.client.delete(f'{BASE}/conversations/{chat}/messages', headers=HEADERS)
        self.assertFalse((self.service.images.root/key).exists())
        self.client.post(BASE+'/auth/guest', headers=HEADERS)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_blog_owner_session_reaches_only_java_publish_request(self):
        """验证 Cookie 穿过真实 SSE 和工具执行线程，不进入响应和聊天历史。"""
        import httpx
        from evan_agent.auth import AuthContext
        from evan_agent.blog_author import BLOG_COOKIE, publish_blog
        from evan_agent.persona import PersonaMiddleware
        self.login()
        self.client.cookies.set(BLOG_COOKIE, 'sample-admin-session')
        chat = self.chat()
        args = dict(title='标题', title_en='Title', excerpt='摘要', excerpt_en='About',
                    markdown='正文', markdown_en='Body', category_id=2, tag_ids=[9])
        model = StreamingToolModel(responses=[
            AIMessage(content='', tool_calls=[dict(name='publish_blog', args=args, id='post', type='tool_call')]),
            AIMessage(content='发布完成。')])
        self.service._agent = create_agent(model=model, tools=[publish_blog], middleware=[PersonaMiddleware()],
                                          context_schema=AuthContext, checkpointer=self.service.sessions.checkpointer)
        posts = []
        def respond(req):
            if req.url.path.endswith('/filters'):
                return httpx.Response(200, json={'categories': [{'id': 2}], 'tags': [{'id': 9}]})
            if req.method == 'GET':
                return httpx.Response(404)
            self.assertEqual(req.url.path, '/api/articles')
            self.assertEqual(req.headers.get('cookie'), BLOG_COOKIE + '=sample-admin-session')
            posts.append(json.loads(req.content))
            return httpx.Response(200, json=posts[-1])
        client_class = httpx.Client
        with patch.dict('os.environ', {'BLOG_ADMIN_USERNAME': '', 'BLOG_ADMIN_PASSWORD': ''}), patch(
                'evan_agent.blog_author.httpx.Client', side_effect=lambda **kw: client_class(
                    transport=httpx.MockTransport(respond), **kw)):
            response = self.client.post(f'{BASE}/conversations/{chat}/messages', headers=HEADERS,
                                        json={'message': '发布博客：测试 Cookie 发布'})
        self.assertEqual(events(response)[-1]['type'], 'done')
        self.assertEqual(len(posts), 1)
        self.assertNotIn('sample-admin-session', response.text)
        self.assertNotIn('sample-admin-session', str(self.service.sessions._items[chat].messages))

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
