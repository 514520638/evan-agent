"""用临时数据库验证清理边界；不修改真实资料或日志。"""
from contextlib import closing
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import time
import unittest
from unittest.mock import patch

from evan_agent.auth import AuthContext
from evan_agent.http_auth import WebSessions
from evan_agent.sessions import SessionStore
from evan_agent.knowledge import KnowledgeStore
from evan_agent.retrieval import Retriever
from evan_agent.maintenance import Maintenance, prune_audit


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / 'chat.sqlite3')
        self.addCleanup(self.store.close)
        self.web = WebSessions(self.store)

    def test_expiry_removes_guest_keeps_owner_and_defers_busy(self):
        guest = AuthContext.guest()
        owner = AuthContext('owner-session', 'owner', 'owner', time.monotonic() + 100)
        token, age = self.web.issue(guest)
        self.assertEqual(age, 3600)
        self.web.issue(owner)
        chat = self.store.create(guest)
        owner_chat = self.store.create(owner)
        for item in self.web._items.values():
            item.expires = 0
        self.store._items[chat].busy = True
        self.web.cleanup_expired()
        self.assertIn(chat, self.store._items)
        with self.assertRaises(PermissionError):
            self.web.require(token)
        self.store._items[chat].busy = False
        self.web.cleanup_expired()
        self.assertNotIn(chat, self.store._items)
        self.assertIn(owner_chat, self.store._items)
        with closing(sqlite3.connect(self.store.database.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM web_sessions').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM conversations').fetchone()[0], 1)

    def test_audit_age_boundary_and_unknown_files(self):
        for name in ['2026-05-02.jsonl', '2026-05-03.jsonl', '2026-11-02.jsonl', 'notes.jsonl', '2025-99-99.jsonl']:
            (self.root / name).write_text('test')
        self.assertEqual(prune_audit(self.root, date(2026, 11, 2)), 1)
        self.assertFalse((self.root / '2026-05-02.jsonl').exists())
        self.assertTrue((self.root / '2026-05-03.jsonl').exists())
        self.assertTrue((self.root / 'notes.jsonl').exists())

    def test_model_switch_removes_old_vectors_only(self):
        knowledge = KnowledgeStore(self.root / 'knowledge.sqlite3')
        with patch.dict('os.environ', {'EMBEDDING_BASE_URL': 'http://example.test/v1', 'EMBEDDING_MODEL': 'old'}):
            old = Retriever(knowledge)
            with closing(sqlite3.connect(old.path)) as db, db:
                db.execute('INSERT INTO vectors VALUES (?,?,?,?)', ('chunk', old.embedder.fingerprint, 'entry', '[1,0]'))
            # 相同配置保留已生成的缓存。
            Retriever(knowledge)
            with closing(sqlite3.connect(old.path)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM vectors').fetchone()[0], 1)
        with patch.dict('os.environ', {'EMBEDDING_BASE_URL': 'http://example.test/v1', 'EMBEDDING_MODEL': 'new'}):
            Retriever(knowledge)
        with closing(sqlite3.connect(old.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM vectors').fetchone()[0], 0)

    def test_background_runs_and_stops(self):
        from threading import Event
        ran = Event()
        maintenance = Maintenance(self.web, KnowledgeStore(self.root / 'k.sqlite3'), self.root, interval=0.01)
        with patch.object(maintenance, 'sweep', side_effect=ran.set):
            maintenance.start()
            self.assertTrue(ran.wait(1))
            maintenance.close()
        self.assertFalse(maintenance.thread.is_alive())
