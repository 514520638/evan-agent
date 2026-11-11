"""验证搜索协议兼容、来源过滤和有界续接，无真实模型费用。"""
import unittest
from unittest.mock import patch
import httpx
from evan_agent.web_search import web_search_request


class WebSearchTests(unittest.TestCase):
    def test_footer_uses_only_current_search_results(self):
        import json
        from langchain_core.messages import ToolMessage
        from evan_agent.web_search import source_footer
        old = ToolMessage(id='old', tool_call_id='a', name='web_search', content=json.dumps({'results': [{'url': 'https://old.test/'}]}))
        new = ToolMessage(id='new', tool_call_id='b', name='web_search', content=json.dumps({'results': [{'url': 'https://python.org/'}, {'url': 'javascript:alert(1)'}]}))
        footer = source_footer([old, new], {'old'})
        self.assertIn('https://python.org/', footer)
        self.assertNotIn('old.test', footer)
        self.assertNotIn('javascript:', footer)

    def request(self, handler):
        real_client = httpx.Client
        with patch.dict('os.environ', {'WEB_SEARCH_ENABLED': 'true', 'MINIMAX_BASE_URL': 'https://example.test/anthropic'}):
            with patch('evan_agent.web_search.httpx.Client', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)):
                return web_search_request('Python 官网')

    def test_result_filter_and_pause_continuation(self):
        import json
        calls = []
        def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            self.assertEqual(request.url.path, '/anthropic/v1/messages')
            return httpx.Response(200, json={'stop_reason': 'pause_turn' if len(calls) == 1 else 'end_turn', 'content': [
                {'type': 'web_search_tool_result', 'content': [
                    {'type': 'web_search_result', 'title': 'Python', 'url': 'https://python.org/', 'content': '官方资料'},
                    {'type': 'web_search_result', 'url': 'https://user@python.org/'},
                    {'type': 'web_search_result', 'url': 'javascript:alert(1)'},
                ]}]})
        result = self.request(handler)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]['messages'][-1]['role'], 'assistant')

    def test_pause_limit_and_errors(self):
        calls = []
        def handler(request):
            calls.append(1)
            return httpx.Response(200, json={'stop_reason': 'pause_turn', 'content': []})
        self.assertEqual(self.request(handler)['status'], 'partial')
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.request(lambda r: httpx.Response(401))['status'], 'unavailable')
        self.assertEqual(web_search_request('')['status'], 'invalid_query')
