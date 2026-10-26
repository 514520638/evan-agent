"""认证边界的离线检查：使用临时数据库，不读取真实 .env 或调用模型。"""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import os
import tempfile
import unittest
from unittest.mock import patch

from evan_agent.auth import (
    AuthContext, AuthenticationError, AuthStore, read_password, require_owner,
)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "auth.sqlite3"
        self.store = AuthStore(self.path)
        self.password = "test-only-password-2026"

    def test_missing_owner_fails_closed(self):
        with self.assertRaises(AuthenticationError):
            self.store.login(self.password)
        with self.assertRaises(PermissionError):
            require_owner(AuthContext.guest())
        with self.assertRaises(PermissionError):
            require_owner(None)

    def test_initialize_login_and_expiry(self):
        self.store.initialize(self.password)
        identity = self.store.login(self.password)
        self.assertEqual(require_owner(identity), "owner")
        with self.assertRaises(PermissionError):
            require_owner(replace(identity, expires_at=0))
        self.assertNotEqual(identity.session_id, self.store.login(self.password).session_id)

    def test_short_password_and_overwrite_rejected(self):
        with self.assertRaises(AuthenticationError):
            self.store.initialize("short")
        self.store.initialize(self.password)
        with self.assertRaises(AuthenticationError):
            self.store.initialize("different-long-password")
        self.assertTrue(self.store.login(self.password).is_owner)

    def test_wrong_password_and_persistent_lockout(self):
        self.store.initialize(self.password)
        for _ in range(5):
            with self.assertRaises(AuthenticationError):
                AuthStore(self.path).login("incorrect-password")
        with self.assertRaisesRegex(AuthenticationError, "锁定"):
            AuthStore(self.path).login(self.password)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE owner SET locked_until=1")
        self.assertTrue(self.store.login(self.password).is_owner)

    def test_success_resets_failure_counter(self):
        self.store.initialize(self.password)
        with self.assertRaises(AuthenticationError):
            self.store.login("incorrect-password")
        self.store.login(self.password)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT failures FROM owner").fetchone()[0], 0)

    def test_password_not_stored_and_random_salt(self):
        self.store.initialize(self.password)
        other = AuthStore(Path(self.temp.name) / "other.sqlite3")
        other.initialize(self.password)
        with closing(sqlite3.connect(self.path)) as first, closing(sqlite3.connect(other.path)) as second:
            a = first.execute("SELECT salt, digest FROM owner").fetchone()
            b = second.execute("SELECT salt, digest FROM owner").fetchone()
        self.assertNotEqual(a, b)
        self.assertNotIn(self.password.encode(), self.path.read_bytes())

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits only; Windows ACL is not verified")
    def test_credentials_file_has_owner_only_posix_permissions(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_password_pipe_is_rejected(self):
        with patch("evan_agent.auth.sys.stdin.isatty", return_value=False):
            with self.assertRaises(AuthenticationError):
                read_password("Password: ")


if __name__ == "__main__":
    unittest.main()
