"""重建服务实例验证 SQLite 聊天、Cookie 和审批恢复，不调用真实模型。"""
from pathlib import Path
from copy import copy
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
import time

from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from evan_agent.auth import AuthContext, AuthStore
from evan_agent.api import create_app
from evan_agent.sessions import SessionStore
from evan_agent.service import AgentService
from evan_agent.knowledge import KnowledgeStore
from evan_agent.middleware import build_middleware, AuditWriter
from evan_agent.knowledge_tools import build_knowledge_tools
from test_blog_flow import FlowModel, decode

H={'X-Agent-Request':'1'}
B='/agent-api'

class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.auth=AuthStore(self.root/'auth.sqlite3');self.auth.initialize('persistence-password')
        self.store=None;self.client=None
        self.addCleanup(self.close)
        self.restart()

    def close(self):
        if self.client: self.client.close();self.client=None
        if self.store: self.store.close();self.store=None

    def restart(self, cookies=None):
        self.close()
        self.store=SessionStore(self.root/'conversations.sqlite3')
        def factory(knowledge,sessions):
            model=FlowModel(responses=[AIMessage(content='unused')])
            return create_agent(model=model,tools=build_knowledge_tools(knowledge),context_schema=AuthContext,
                checkpointer=sessions.checkpointer,middleware=build_middleware(model,writer=AuditWriter(self.root/'audit'),lifecycle_logs=False))
        service=AgentService(self.auth,KnowledgeStore(self.root/'knowledge.sqlite3'),self.store,factory)
        with patch('evan_agent.api.load_dotenv'),patch.dict('os.environ',{'AGENT_COOKIE_SECURE':'false'}):
            app=create_app(service)
        self.client=TestClient(app)
        if cookies: self.client.cookies.update(cookies)

    def login(self):
        self.assertEqual(self.client.post(B+'/auth/login',headers=H,json={'password':'persistence-password'}).status_code,200)

    def test_restart_resumes_edited_approval_then_keeps_history(self):
        self.login()
        chat=self.client.post(B+'/conversations',headers=H).json()['conversation_id']
        proposed=decode(self.client.post(f'{B}/conversations/{chat}/messages',headers=H,json={'message':'记住，我在北京工作'}))[-1]
        approval=proposed['approval_id'];url=f'{B}/conversations/{chat}/approvals/{approval}'
        decision={'decisions':[{'type':'edit','changes':{'content':'我在北京工作，公司：重启测试公司','visibility':'public','status':'published'}}]}
        self.assertEqual(self.client.patch(url+'/draft',headers=H,json=decision).status_code,200)
        cookies=self.client.cookies
        self.restart(cookies)
        self.assertEqual(self.client.get(B+'/auth/me').json()['role'],'owner')
        restored=self.client.get(f'{B}/conversations/{chat}').json()
        self.assertEqual(restored['pending']['draft_decisions'],decision['decisions'])
        result=decode(self.client.post(url,headers=H,json=decision))[-1]
        self.assertEqual(result['type'],'done')
        self.assertEqual(len(result['writes']),1)
        self.restart(cookies)
        history=self.client.get(f'{B}/conversations/{chat}').json()
        self.assertTrue(history['messages'])
        self.assertIsNone(history['pending'])
        self.assertEqual(self.client.post(url,headers=H,json=decision).status_code,409)
        self.assertEqual(len(KnowledgeStore(self.root/'knowledge.sqlite3').list_entries(self.auth.login('persistence-password'))),1)

    def test_logout_relogin_recovers_owner_but_revokes_old_cookie(self):
        self.login();chat=self.client.post(B+'/conversations',headers=H).json()['conversation_id']
        old=type(self.client.cookies)()
        for cookie in self.client.cookies.jar:
            old.jar.set_cookie(copy(cookie))
        self.client.post(B+'/auth/logout',headers=H)
        self.restart(old)
        self.assertEqual(self.client.get(B+'/auth/me').status_code,401)
        self.login()
        self.assertEqual(self.client.get(f'{B}/conversations/{chat}').status_code,200)

    def test_guest_cookie_survives_restart_without_cross_access(self):
        self.client.post(B+'/auth/guest',headers=H)
        chat=self.client.post(B+'/conversations',headers=H).json()['conversation_id']
        cookies=self.client.cookies
        self.restart(cookies)
        self.assertEqual(self.client.get(f'{B}/conversations/{chat}').status_code,200)
        self.client.post(B+'/auth/guest',headers=H)
        self.assertEqual(self.client.get(f'{B}/conversations/{chat}').status_code,403)

    def test_running_turn_is_not_replayed_and_second_process_is_rejected(self):
        identity=AuthContext.guest();chat=self.store.create(identity)
        item=self.store._items[chat];item.busy=True
        item.operation={'id':'test','thread_id':'interrupted-thread','status':'running','writes':[]}
        self.store._persist(chat,item)
        with self.assertRaises(RuntimeError): SessionStore(self.root/'conversations.sqlite3')
        self.restart()
        state=self.store.operation_for(identity,chat)
        self.assertFalse(state['running'])
        self.assertEqual(state['operation']['status'],'interrupted')
        self.assertIsNone(self.store.pending_for(identity,chat))

if __name__=='__main__': unittest.main()
