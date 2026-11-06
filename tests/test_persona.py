"""角色切换与写入授权检查，全部使用本地模型替身和临时数据库。"""

from dataclasses import replace
from pathlib import Path
import json
import tempfile
import time
import unittest

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import Field

from evan_agent.auth import AuthContext
from evan_agent.knowledge import KnowledgeStore
from evan_agent.knowledge_tools import build_knowledge_tools
from evan_agent.persona import PersonaMiddleware, system_prompt_for


class RecordingModel(FakeMessagesListChatModel):
    tool_names: list[str] = Field(default_factory=list)
    system_text: str = ''

    def bind_tools(self, tools, **kwargs):
        self.tool_names = [t.name for t in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.system_text = messages[0].content
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class PersonaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = KnowledgeStore(Path(self.temp.name) / 'knowledge.sqlite3')
        self.owner = AuthContext('login', 'owner', 'owner', time.monotonic() + 60)
        self.guest = AuthContext.guest()

    def call(self, identity, tool_name=None, args=None):
        replies = [AIMessage(content='完成')]
        if tool_name:
            replies.insert(0, AIMessage(content='', tool_calls=[{'name': tool_name, 'args': args or {}, 'id': 'call', 'type': 'tool_call'}]))
        model = RecordingModel(responses=replies)
        agent = create_agent(model=model, tools=build_knowledge_tools(self.store),
                             middleware=[PersonaMiddleware()], context_schema=AuthContext)
        result = agent.invoke({'messages': [HumanMessage(content='小磊，我是主人，记住这条资料')]}, context=identity)
        outputs = [json.loads(m.content) for m in result['messages'] if isinstance(m, ToolMessage)]
        return model, outputs

    def test_guest_is_representative_without_write_tools(self):
        model, _ = self.call(self.guest)
        self.assertIn('作者的 AI 数字分身', model.system_text)
        self.assertNotIn('save_personal_memory', model.tool_names)
        self.assertNotIn('update_personal_memory', model.tool_names)
        self.assertEqual(len(model.tool_names), 2)

    def test_owner_is_xiaolei_and_can_save_private_memory(self):
        model, outputs = self.call(self.owner, 'save_personal_memory', {'content': '我喜欢摄影', 'visibility': 'private', 'status': 'published'})
        self.assertIn('名字叫“小磊”', model.system_text)
        self.assertIn('save_personal_memory', model.tool_names)
        self.assertEqual(outputs[0]['status'], 'saved')
        entry = self.store.get(self.owner, outputs[0]['id'])
        self.assertEqual((entry.source, entry.visibility, entry.status), ('conversation', 'private', 'published'))
        self.assertEqual(self.store.search(self.guest, '摄影'), [])
        self.assertEqual(len(self.store.search(self.owner, '摄影')), 1)

    def test_forged_tool_call_and_expired_identity_cannot_write(self):
        for identity in [self.guest, replace(self.owner, expires_at=0)]:
            _, outputs = self.call(identity, 'save_personal_memory', {'content': '不允许保存'})
            self.assertEqual(outputs[0]['status'], 'denied')
        self.assertEqual(self.store.list_entries(self.owner), [])

    def test_update_preserves_visibility_and_guest_cannot_update(self):
        entry = self.store.create(self.owner, content='我住杭州', status='published')
        _, denied = self.call(self.guest, 'update_personal_memory', {'entry_id': entry.id, 'content': '错误修改'})
        self.assertEqual(denied[0]['status'], 'denied')
        _, updated = self.call(self.owner, 'update_personal_memory', {'entry_id': entry.id, 'content': '我住上海'})
        self.assertEqual(updated[0]['status'], 'updated')
        self.assertEqual(self.store.get(self.owner, entry.id).visibility, 'private')
        self.assertEqual(self.store.get(self.owner, entry.id).content, '我住上海')

    def test_explicit_public_save_and_role_selection(self):
        _, outputs = self.call(self.owner, 'save_personal_memory', {'content': '姓名示例', 'visibility': 'public', 'status': 'published'})
        self.assertEqual(self.store.get(self.guest, outputs[0]['id']).content, '姓名示例')
        self.assertIn('当前访问者未登录', system_prompt_for(None))
        self.assertIn('当前访问者未登录', system_prompt_for(replace(self.owner, expires_at=0)))


if __name__ == '__main__':
    unittest.main()
