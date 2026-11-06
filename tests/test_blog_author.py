"""使用临时身份和模拟 Java 接口验证发布权限、文章格式与重复调用。"""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from langchain_core.messages import HumanMessage
from evan_agent.auth import AuthContext
from evan_agent.blog_author import publish_blog, wants_publication, SEPARATOR


class BlogAuthorTests(unittest.TestCase):
    def test_intent(self):
        self.assertTrue(wants_publication('帮我发布一篇博客 用于解释工作流是什么，在agent中的作用，以及如何实现，实现原理等知识'))
        self.assertTrue(wants_publication('发布博客：工作流是什么？'))
        self.assertTrue(wants_publication('帮我写一篇 RAG 文档并发布到我的博客'))
        self.assertFalse(wants_publication('写一篇 RAG 文档'))
        self.assertFalse(wants_publication('发布博客：先不要发布这篇文章'))
        self.assertTrue(wants_publication('写一篇介绍rag原理的博客放到博客文章里面'))
        self.assertTrue(wants_publication('把刚才的文章发布到博客'))
        self.assertFalse(wants_publication('写一篇介绍rag原理的博客，不要放到博客文章里面'))
        self.assertFalse(wants_publication('如何把文章发布到博客？'))

    def test_actual_owner_request_exposes_tool(self):
        from langchain.agents import create_agent
        from langchain_core.messages import AIMessage
        from test_persona import RecordingModel
        from evan_agent.persona import PersonaMiddleware
        model = RecordingModel(responses=[AIMessage(content='准备生成')])
        agent = create_agent(model=model, tools=[publish_blog], middleware=[PersonaMiddleware()], context_schema=AuthContext)
        question = HumanMessage(content='写一篇介绍rag原理的博客放到博客文章里面')
        owner = AuthContext('s', 'owner', 'owner', time.monotonic()+60)
        agent.invoke({'messages': [question]}, context=owner)
        self.assertIn('publish_blog', model.tool_names)
        model.tool_names = []
        agent.invoke({'messages': [question]}, context=AuthContext.guest())
        self.assertNotIn('publish_blog', model.tool_names)

    def test_publish_then_retry(self):
        turn = HumanMessage(content='发布博客：介绍混合检索', id='message-1')
        runtime = SimpleNamespace(context=AuthContext.guest(), state={'messages': [turn]})
        args = ('混合检索', 'Hybrid retrieval', '中文摘要', 'Summary', '中文正文', 'English body', 2, [9], runtime)
        with self.assertRaises(PermissionError):
            publish_blog.func(*args)
        runtime.context = AuthContext('s', 'owner', 'owner', time.monotonic()+60)
        articles = []
        def respond(request):
            import json
            path = request.url.path
            if path.endswith('/filters'):
                return httpx.Response(200, json={'categories':[{'id':2}], 'tags':[{'id':9}]})
            if path.endswith('/login'):
                return httpx.Response(200, json={'success':True})
            if path == '/api/articles':
                articles.append(json.loads(request.content))
                return httpx.Response(200, json=articles[-1])
            return httpx.Response(200, json=articles[0]) if articles else httpx.Response(404)
        client = httpx.Client
        with patch.dict('os.environ', {'BLOG_ADMIN_USERNAME':'test','BLOG_ADMIN_PASSWORD':'test'}), patch('evan_agent.blog_author.httpx.Client', side_effect=lambda **kw: client(transport=httpx.MockTransport(respond),**kw)):
            self.assertEqual(publish_blog.func(*args)['status'], 'published')
            self.assertEqual(publish_blog.func(*args)['status'], 'already_published')
            self.assertEqual(len(articles), 1)
            self.assertEqual(articles[0]['content'], '中文正文'+SEPARATOR+'English body')
