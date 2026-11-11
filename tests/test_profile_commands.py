"""资料入口的离线交互测试，使用临时数据库和模拟输入。"""

from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from evan_agent.auth import AuthContext
from evan_agent.knowledge import KnowledgeStore, EntryNotFoundError
from evan_agent.profile_commands import ProfileCommands


class ProfileCommandsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = KnowledgeStore(Path(self.temp.name) / 'knowledge.sqlite3')
        self.commands = ProfileCommands(self.store)
        self.owner = AuthContext('login', 'owner', 'owner', time.monotonic() + 60)
        self.guest = AuthContext.guest()

    def run_command(self, command, answers=(), identity=None):
        with patch('builtins.input', side_effect=answers), redirect_stdout(io.StringIO()) as output:
            consumed = self.commands.handle(command, identity or self.owner)
        return consumed, output.getvalue()

    def test_add_defaults_multiline_and_confirm(self):
        self.run_command('/profile add', ['第一行', '第二行', '/end', '', '', '', '保存'])
        item = self.store.list_entries(self.owner)[0]
        self.assertEqual(item.content, '第一行\n第二行')
        self.assertEqual((item.visibility, item.status, item.source), ('private', 'draft', 'manual'))

    def test_cancel_and_guest_write_do_not_save(self):
        self.run_command('/profile add', ['/cancel'])
        self.run_command('/profile add', ['内容', '/end', '', '', '', '取消'])
        _, output = self.run_command('/profile add', identity=self.guest)
        self.assertIn('本人登录', output)
        self.assertEqual(self.store.list_entries(self.owner), [])

    def test_edit_publish_and_guest_read(self):
        item = self.store.create(self.owner, content='原文')
        self.run_command(f'/profile edit {item.id}', ['', 'work', '公开', '已发布', '保存'])
        self.assertEqual(self.store.get(self.guest, item.id).content, '原文')
        _, output = self.run_command(f'/profile show {item.id}', identity=self.guest)
        self.assertIn('原文', output)
        private = self.store.create(self.owner, content='不应泄露的资料')
        _, output = self.run_command(f'/profile show {private.id}', identity=self.guest)
        self.assertNotIn('不应泄露的资料', output)

    def test_delete_requires_confirmation(self):
        item = self.store.create(self.owner, content='资料')
        self.run_command(f'/profile delete {item.id}', ['取消'])
        self.assertEqual(self.store.get(self.owner, item.id).content, '资料')
        self.run_command(f'/profile delete {item.id}', ['删除'])
        with self.assertRaises(EntryNotFoundError):
            self.store.get(self.owner, item.id)

    def test_expired_identity_and_bad_commands(self):
        _, output = self.run_command('/profile add', identity=replace(self.owner, expires_at=0))
        self.assertIn('本人登录', output)
        for command in ['/profile list 0', '/profile list abc', '/profile show', '/profile "bad']:
            consumed, _ = self.run_command(command)
            self.assertTrue(consumed)
        self.assertFalse(self.commands.handle('普通聊天', self.owner))

    def test_pagination(self):
        for i in range(11):
            self.store.create(self.owner, content=f'条目 {i}')
        _, page1 = self.run_command('/profile list')
        self.assertIn('/profile list 2', page1)
        _, page2 = self.run_command('/profile list 2')
        self.assertEqual(page2.count('private/draft'), 1)


if __name__ == '__main__':
    unittest.main()
