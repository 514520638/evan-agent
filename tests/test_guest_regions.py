"""地区估算：迁移兼容、非公网过滤、后台查询、缓存和失败降级。"""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone
import httpx
from evan_agent.guest_questions import GuestQuestions

class GuestRegionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'questions.sqlite3'
    def test_old_rows_survive_migration_and_have_unknown_region(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE guest_questions(id INTEGER PRIMARY KEY,created_at TEXT NOT NULL,question TEXT NOT NULL)')
            db.execute("INSERT INTO guest_questions VALUES (1,'2026-11-01T00:00:00+00:00','旧问题')")
        row = GuestQuestions(self.path).list()['items'][0]
        self.assertEqual(row['question'], '旧问题'); self.assertEqual(row['geo_status'], 'unknown'); self.assertIsNone(row['region'])
    def test_question_write_does_not_wait_for_external_lookup(self):
        store = GuestQuestions(self.path)
        with patch('evan_agent.guest_questions.httpx.Client', side_effect=AssertionError('No lookup on write')):
            store.add('你好', '8.8.8.8')
        self.assertEqual(store.list()['items'][0]['geo_status'], 'pending')
    def test_region_is_estimated_and_raw_ip_removed_and_lookup_cached(self):
        store = GuestQuestions(self.path); store.add('问题一','8.8.8.8'); store.add('问题二','8.8.8.8')
        client = httpx.Client; requests = []
        def respond(request):
            requests.append(str(request.url)); return httpx.Response(200,json={'success':True,'country':'测试国家','region':'测试省','city':'测试市','latitude':1})
        with patch('evan_agent.guest_questions.httpx.Client', side_effect=lambda **kw: client(transport=httpx.MockTransport(respond),**kw)):
            store.resolve_one(); store.resolve_one()
        self.assertEqual(len(requests),1)
        row=store.list()['items'][0]; self.assertEqual(row['region'],'测试国家 · 测试省 · 测试市'); self.assertNotIn('client_ip',row); self.assertNotIn('latitude',row)
        with sqlite3.connect(self.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM guest_questions WHERE client_ip IS NOT NULL').fetchone()[0],0)
    def test_private_ip_and_failed_lookup_stay_unknown(self):
        store=GuestQuestions(self.path); store.add('私网','127.0.0.1'); self.assertFalse(store.resolve_one())
        store.add('失败','8.8.4.4')
        with patch('evan_agent.guest_questions.httpx.Client', side_effect=httpx.ConnectError('offline')):store.resolve_one()
        self.assertTrue(all(row['geo_status']=='unknown' for row in store.list()['items']))
    def test_daily_budget_prevents_external_calls(self):
        store=GuestQuestions(self.path); store.add('限额','8.8.8.8')
        with sqlite3.connect(self.path) as db:db.execute('INSERT INTO geo_budget VALUES (?,900)',(datetime.now(timezone.utc).date().isoformat(),))
        with patch('evan_agent.guest_questions.httpx.Client', side_effect=AssertionError('Budget exceeded')):store.resolve_one()
        self.assertEqual(store.list()['items'][0]['geo_status'],'unknown')

    def test_disabled_lookup_discards_pending_ip_without_external_call(self):
        store=GuestQuestions(self.path); store.add('等待定位','8.8.8.8')
        with patch.dict('os.environ', {'GUEST_GEO_ENABLED':'false'}), patch('evan_agent.guest_questions.httpx.Client', side_effect=AssertionError('Disabled')):
            self.assertFalse(store.resolve_one())
        self.assertEqual(store.list()['items'][0]['geo_status'], 'unknown')
        with sqlite3.connect(self.path) as db:self.assertIsNone(db.execute('SELECT client_ip FROM guest_questions').fetchone()[0])
