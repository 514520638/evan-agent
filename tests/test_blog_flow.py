"""完整 HTTP 业务链路验收：虚构资料、临时数据库，模型替身只控制工具选择。"""

from contextlib import closing
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatResult, ChatGeneration

from evan_agent.api import create_app
from evan_agent.auth import AuthContext, AuthStore
from evan_agent.knowledge import KnowledgeStore
from evan_agent.knowledge_tools import build_knowledge_tools
from evan_agent.middleware import build_middleware, AuditWriter
from evan_agent.service import AgentService
from evan_agent.sessions import SessionStore
from test_streaming import StreamingToolModel

BASE = '/agent-api'
HEADERS = {'X-Agent-Request': '1', 'Origin': 'https://evanlink.cn'}


def decode(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


class FlowModel(StreamingToolModel):
    """真实工具节点和数据库照常运行，仅替换远端模型网络请求。"""
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if isinstance(messages[-1], ToolMessage):
            text = messages[-1].content
            if messages[-1].name == 'search_personal_knowledge':
                data = json.loads(text)
                text = '\n'.join(item['content'] + item['citation'] for item in data.get('results', [])) or '没有找到可查询资料'
            else:
                text = '资料处理完成，请核对数据库回执。'
            answer = AIMessage(content=text)
        elif '记住' in messages[-1].text:
            answer = AIMessage(content='', tool_calls=[{'name': 'save_personal_memory', 'args': {'content': '我在北京工作'}, 'id': 'flow-save', 'type': 'tool_call'}])
        else:
            answer = AIMessage(content='', tool_calls=[{'name': 'search_personal_knowledge', 'args': {'query': '工作'}, 'id': 'flow-read', 'type': 'tool_call'}])
        return ChatResult(generations=[ChatGeneration(message=answer)])


class BlogFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        auth = AuthStore(self.root/'auth.sqlite3')
        auth.initialize('flow-password-only')
        def factory(store, sessions):
            model = FlowModel(responses=[AIMessage(content='unused')])
            return create_agent(model=model, tools=build_knowledge_tools(store), context_schema=AuthContext,
                checkpointer=sessions.checkpointer, middleware=build_middleware(model,
                writer=AuditWriter(self.root/'audit'), lifecycle_logs=False))
        self.service = AgentService(auth, KnowledgeStore(self.root/'knowledge.sqlite3'), SessionStore(), factory)
        with patch('evan_agent.api.load_dotenv'), patch.dict('os.environ', {
            'AGENT_ALLOWED_ORIGINS': 'https://evanlink.cn', 'AGENT_COOKIE_SECURE': 'true',
        }):
            self.app = create_app(self.service)
        self.owner = TestClient(self.app, base_url='https://evanlink.cn')
        self.guest = TestClient(self.app, base_url='https://evanlink.cn')
        self.addCleanup(self.owner.close)
        self.addCleanup(self.guest.close)
        self.owner.post(BASE+'/auth/login', headers=HEADERS, json={'password':'flow-password-only'})
        self.guest.post(BASE+'/auth/guest', headers=HEADERS)

    def new_chat(self, client):
        return client.post(BASE+'/conversations', headers=HEADERS).json()['conversation_id']

    def propose(self):
        chat = self.new_chat(self.owner)
        r = self.owner.post(f'{BASE}/conversations/{chat}/messages', headers=HEADERS, json={'message':'记住，我在北京工作'})
        event = decode(r)[-1]
        self.assertEqual(event['type'], 'approval_required')
        return chat, event

    def test_supplement_save_database_guest_query_and_private_filter(self):
        for visibility, status in [('public','published'), ('private','published'), ('public','draft')]:
            chat, pending = self.propose()
            text = f'我在北京工作。公司：联调示例-{visibility}-{status}；职位：工程师；行业：软件'
            url = f"{BASE}/conversations/{chat}/approvals/{pending['approval_id']}"
            decision = {'decisions':[{'type':'edit','changes':{'content':text,'visibility':visibility,'status':status}}]}
            # 暂存只影响审批表单，不能写入资料库。
            prior = len(self.service.knowledge.list_entries(self.service.sessions._items[chat].owner))
            self.assertEqual(self.owner.patch(url+'/draft',headers=HEADERS,json=decision).status_code,200)
            self.assertEqual(len(self.service.knowledge.list_entries(self.service.sessions._items[chat].owner)),prior)
            refreshed = self.owner.get(f'{BASE}/conversations/{chat}').json()
            self.assertEqual(refreshed['pending']['draft_decisions'],decision['decisions'])
            self.assertEqual(refreshed['pending']['user_message'],'记住，我在北京工作')
            result = decode(self.owner.post(url,headers=HEADERS,json=decision))[-1]
            self.assertEqual(result['type'],'done')
            self.assertEqual(len(result['writes']),1)
            receipt = result['writes'][0]
            row = KnowledgeStore(self.root/'knowledge.sqlite3').get(self.service.sessions._items[chat].owner,receipt['id'])
            self.assertEqual((row.content,row.visibility,row.status),(text,visibility,status))
            self.assertEqual(self.owner.post(url,headers=HEADERS,json=decision).status_code,409)
        visitor_chat = self.new_chat(self.guest)
        reply = decode(self.guest.post(f'{BASE}/conversations/{visitor_chat}/messages', headers=HEADERS,json={'message':'你在哪工作？'}))[-1]['answer']
        self.assertIn('联调示例-public-published',reply)
        self.assertNotIn('联调示例-private',reply)
        self.assertNotIn('联调示例-public-draft',reply)

    def test_https_cookie_refresh_and_revocation(self):
        login = self.owner.post(BASE+'/auth/login',headers=HEADERS,json={'password':'flow-password-only'})
        cookie = login.headers['set-cookie']
        for flag in ['HttpOnly','Secure','SameSite=lax','Path=/agent-api']:
            self.assertIn(flag,cookie)
        with TestClient(self.app,base_url='https://evanlink.cn') as refreshed:
            refreshed.cookies.update(self.owner.cookies)
            self.assertEqual(refreshed.get(BASE+'/auth/me').json()['role'],'owner')
            chat, pending = self.propose()
            self.assertEqual(refreshed.get(f'{BASE}/conversations/{chat}').json()['pending']['approval_id'],pending['approval_id'])
            self.owner.post(BASE+'/auth/logout',headers=HEADERS)
            self.assertEqual(refreshed.get(BASE+'/auth/me').status_code,401)
        blocked = self.guest.post(BASE+'/auth/guest',headers={**HEADERS,'Origin':'https://evil.invalid'})
        self.assertEqual(blocked.status_code,403)

    def test_disconnect_after_commit_retains_receipt_and_blocks_replay(self):
        chat, pending = self.propose()
        owner = self.service.sessions._items[chat].owner
        decision = self.service.prepare_review(owner,chat,pending['approval_id'],[{'type':'approve'}])
        # 模拟 SSE 已收到首个最终回答片段后连接断开，此时工具已经提交数据库。
        with closing(self.service.sessions.events(self.service.agent,owner,chat,
                     approval_id=pending['approval_id'],decision=decision)) as stream:
            for event in stream:
                if event['type']=='delta':
                    break
        snapshot = self.owner.get(f'{BASE}/conversations/{chat}').json()
        self.assertFalse(snapshot['running'])
        self.assertEqual(snapshot['operation']['status'],'interrupted')
        self.assertEqual(len(snapshot['operation']['writes']),1)
        self.assertEqual(len(self.service.knowledge.list_entries(owner)),1)
        retry = self.owner.post(f"{BASE}/conversations/{chat}/approvals/{pending['approval_id']}", headers=HEADERS,json={'decisions':[{'type':'approve'}]})
        self.assertEqual(retry.status_code,409)
        self.assertEqual(len(self.service.knowledge.list_entries(owner)),1)

    def test_disconnect_before_review_delivery_preserves_pending_without_write(self):
        identity = self.service.auth.login('flow-password-only')
        chat = self.service.sessions.create(identity)
        with closing(self.service.sessions.events(self.service.agent,identity,chat,'记住，我在北京工作')) as stream:
            for event in stream:
                if event['type']=='approval_required':
                    break
        self.assertEqual(self.service.knowledge.list_entries(identity),[])
        self.assertIsNotNone(self.service.sessions.pending_for(identity,chat))
        pending = self.service.sessions.pending_for(identity,chat)
        self.service.sessions.cancel_pending(identity,chat,pending['approval_id'])
        self.assertEqual(self.service.knowledge.list_entries(identity),[])

    def test_draft_cannot_change_identity_or_be_saved_by_guest(self):
        chat,pending=self.propose()
        url=f"{BASE}/conversations/{chat}/approvals/{pending['approval_id']}/draft"
        self.assertEqual(self.owner.patch(url,headers=HEADERS,json={'decisions':[{'type':'edit','changes':{'owner_id':'someone'}}]}).status_code,400)
        self.assertEqual(self.guest.patch(url,headers=HEADERS,json={'decisions':[{'type':'approve'}]}).status_code,403)


if __name__=='__main__':
    unittest.main()
