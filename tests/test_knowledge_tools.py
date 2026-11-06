"""通过真实 Agent 工具节点验证检索和身份注入，模型使用本地替身。"""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from evan_agent.auth import AuthContext
from evan_agent.knowledge import KnowledgeStore
from evan_agent.knowledge_tools import build_knowledge_tools


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class KnowledgeToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = KnowledgeStore(Path(self.temp.name) / 'knowledge.sqlite3')
        self.owner = AuthContext('owner-login', 'owner', 'owner', time.monotonic() + 60)
        self.guest = AuthContext.guest()
        self.public = self.store.create(self.owner, content='我在杭州工作，喜欢摄影。', visibility='public', status='published')
        self.private = self.store.create(self.owner, content='私人工作地址', status='published')
        self.draft = self.store.create(self.owner, content='工作规划草稿', visibility='public')
        self.tools = build_knowledge_tools(self.store)

    def call(self, name, args, identity=None):
        model = ToolModel(responses=[
            AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': 'lookup', 'type': 'tool_call'}]),
            AIMessage(content='本地测试结束'),
        ])
        agent = create_agent(model=model, tools=self.tools, context_schema=AuthContext)
        result = agent.invoke({'messages': [HumanMessage(content='查询资料')]}, context=identity or self.guest)
        return json.loads(next(m.content for m in result['messages'] if isinstance(m, ToolMessage)))

    def test_guest_search_and_source(self):
        result = self.call('search_personal_knowledge', {'query': '工作'})
        self.assertEqual([r['id'] for r in result['results']], [self.public.id])
        self.assertEqual(result['results'][0]['citation'], f'[资料:{self.public.id}]')
        self.assertNotIn('私人', str(result))
        self.assertNotIn('草稿', str(result))

    def test_owner_sees_published_private_but_not_drafts(self):
        result = self.call('search_personal_knowledge', {'query': '工作'}, self.owner)
        self.assertEqual({r['id'] for r in result['results']}, {self.public.id, self.private.id})

    def test_guessed_id_expired_identity_and_schema(self):
        for entry in [self.private, self.draft]:
            self.assertEqual(self.call('get_personal_knowledge', {'entry_id': entry.id})['status'], 'not_found')
        result = self.call('search_personal_knowledge', {'query': '工作'}, replace(self.owner, expires_at=0))
        self.assertEqual(result['status'], 'denied')
        for tool in self.tools:
            schema = tool.tool_call_schema.model_json_schema()['properties']
            self.assertNotIn('runtime', schema)
            self.assertNotIn('owner_id', schema)
            self.assertNotIn('role', schema)

    def test_short_chinese_literal_special_characters_and_ranking(self):
        self.assertEqual(self.store.search(self.guest, '杭州')[0].id, self.public.id)
        self.assertEqual(self.store.search(self.guest, '%'), [])
        self.assertEqual(self.store.search(self.guest, "' OR 1=1 --"), [])
        other = self.store.create(self.owner, content='杭州旅游', visibility='public', status='published')
        self.assertEqual(self.store.search(self.guest, '杭州 摄影')[0].id, self.public.id)
        self.assertEqual(len(self.store.search(self.guest, '', limit=1)), 1)

    def test_updates_and_deletes_visible_immediately(self):
        self.store.update(self.owner, self.public.id, content='现在喜欢游泳')
        self.assertEqual(self.store.search(self.guest, '摄影'), [])
        self.assertEqual(self.store.search(self.guest, '游泳')[0].id, self.public.id)
        self.store.delete(self.owner, self.public.id)
        self.assertEqual(self.store.search(self.guest, '游泳'), [])

    def test_pagination_and_long_content(self):
        entry = self.store.create(self.owner, content='前' * 2000 + '登山' + '后' * 5000, visibility='public', status='published')
        result = self.call('search_personal_knowledge', {'query': '登山'})['results'][0]
        self.assertIn('登山', result['content'])
        self.assertTrue(result['truncated'])
        first = self.call('get_personal_knowledge', {'entry_id': entry.id})
        second = self.call('get_personal_knowledge', {'entry_id': entry.id, 'offset': first['next_offset']})
        self.assertEqual(first['content'] + second['content'], entry.content)
        page = self.call('search_personal_knowledge', {'query': '', 'limit': 1})
        self.assertEqual(page['next_offset'], 1)


if __name__ == '__main__':
    unittest.main()
