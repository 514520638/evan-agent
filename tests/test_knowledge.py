"""个人资料存储的离线验证，所有数据写入临时目录。"""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import os
import tempfile
import time
import unittest

from evan_agent.auth import AuthContext
from evan_agent.knowledge import EntryNotFoundError, KnowledgeStore


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'knowledge.sqlite3'
        self.store = KnowledgeStore(self.path)
        self.owner = AuthContext('login', 'owner', 'owner', time.monotonic() + 60)
        self.guest = AuthContext.guest()

    def test_defaults_original_text_and_restart(self):
        item = self.store.create(self.owner, content='  第一行\n第二行  ')
        self.assertEqual((item.owner_id, item.visibility, item.status), ('owner', 'private', 'draft'))
        restored = KnowledgeStore(self.path).get(self.owner, item.id)
        self.assertEqual(restored, item)
        self.assertEqual(restored.content, '  第一行\n第二行  ')

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits only; Windows ACL is not verified")
    def test_knowledge_file_has_owner_only_posix_permissions(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_only_public_published_visible_to_guest(self):
        visible = None
        for visibility in ['private', 'public']:
            for status in ['draft', 'published', 'archived']:
                item = self.store.create(self.owner, content='资料', visibility=visibility, status=status)
                if (visibility, status) == ('public', 'published'):
                    visible = item
                else:
                    with self.assertRaises(EntryNotFoundError):
                        self.store.get(self.guest, item.id)
        self.assertEqual(self.store.list_entries(self.guest), [visible])
        self.assertEqual(len(self.store.list_entries(self.owner)), 6)

    def test_guest_expired_and_wrong_owner_cannot_write(self):
        item = self.store.create(self.owner, content='原始资料')
        for identity in [self.guest, replace(self.owner, expires_at=0), replace(self.owner, user_id='other')]:
            for action in [
                lambda: self.store.create(identity, content='非法录入'),
                lambda: self.store.update(identity, item.id, content='非法修改'),
                lambda: self.store.delete(identity, item.id),
            ]:
                with self.assertRaises(PermissionError):
                    action()
        self.assertEqual(self.store.get(self.owner, item.id).content, '原始资料')
        with self.assertRaises(PermissionError):
            self.store.list_entries(replace(self.owner, expires_at=0))

    def test_update_publish_archive_delete(self):
        item = self.store.create(self.owner, content='旧资料')
        updated = self.store.update(self.owner, item.id, content='新资料', category='work', source='conversation', visibility='public', status='published')
        self.assertEqual(updated.created_at, item.created_at)
        self.assertGreaterEqual(updated.updated_at, item.updated_at)
        self.assertEqual(self.store.get(self.guest, item.id).content, '新资料')
        self.store.update(self.owner, item.id, status='archived')
        self.assertEqual(self.store.list_entries(self.guest), [])
        self.store.delete(self.owner, item.id)
        with self.assertRaises(EntryNotFoundError):
            self.store.get(self.owner, item.id)

    def test_invalid_data_and_owner_change_rejected(self):
        for fields in [{'content': ' \n'}, {'content': 'x', 'visibility': 'all'}, {'content': 'x', 'source': 'unknown'}]:
            with self.assertRaises(ValueError):
                self.store.create(self.owner, **fields)
        item = self.store.create(self.owner, content='有效资料')
        with self.assertRaises(ValueError):
            self.store.update(self.owner, item.id, owner_id='other')
        self.assertEqual(self.store.get(self.owner, item.id).owner_id, 'owner')

    def test_query_values_are_bound_and_pagination(self):
        item = self.store.create(self.owner, content="O'Reilly", category="' OR 1=1 --", visibility='public', status='published')
        self.store.create(self.owner, content='隐藏资料')
        self.assertEqual(self.store.list_entries(self.guest, category="' OR 1=1 --"), [item])
        with self.assertRaises(EntryNotFoundError):
            self.store.get(self.guest, "' OR 1=1 --")
        self.assertEqual(len(self.store.list_entries(self.owner, limit=1, offset=1)), 1)

    def test_database_constraints_and_version(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO profile_entries VALUES ('id', 'owner', 'text', 'general', 'manual', 'bad', 'draft', 'now', 'now')")
            db.execute('PRAGMA user_version=999')
        with self.assertRaises(RuntimeError):
            KnowledgeStore(self.path)


if __name__ == '__main__':
    unittest.main()
