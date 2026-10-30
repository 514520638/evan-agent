"""HTTP 会话：浏览器只持有随机 HttpOnly Cookie，服务端保存身份。

持久化模式保存令牌哈希与绝对过期时间；原始令牌不落盘。
"""

from dataclasses import dataclass
import hashlib
import json
from contextlib import closing
from evan_agent.conversation_db import private_connection
import secrets
from threading import RLock
import time

from evan_agent.auth import AuthContext


@dataclass
class WebSession:
    identity: AuthContext
    expires: float


class WebSessions:
    def __init__(self, conversations):
        self._items = {}
        self._lock = RLock()
        self.conversations = conversations
        self.database = conversations.database
        if self.database:
            with closing(private_connection(self.database.path)) as db:
                rows = db.execute('SELECT token_hash,identity_json,expires_wall FROM web_sessions').fetchall()
            for key, raw, expires_wall in rows:
                fields = json.loads(raw)
                remaining = expires_wall - time.time()
                fields['expires_at'] = time.monotonic() + max(0, remaining) if fields['role'] == 'owner' else 0
                self._items[key] = WebSession(AuthContext(**fields), time.monotonic() + remaining)
                if remaining <= 0:
                    self._remove(key)

    def _remove(self, key):
        if self.database:
            with closing(private_connection(self.database.path)) as db, db:
                db.execute('DELETE FROM web_sessions WHERE token_hash=?', (key,))
        session = self._items.pop(key, None)
        if session:
            session.identity.revoked.set()
            self.conversations.discard_identity(session.identity)

    def issue(self, identity):
        with self._lock:
            now = time.monotonic()
            for key, value in list(self._items.items()):
                if value.expires <= now:
                    self._remove(key)
            if len(self._items) >= 1000:
                raise ValueError('当前会话数量已达上限，请稍后重试。')
            token = secrets.token_urlsafe(32)
            expires = identity.expires_at if identity.is_owner else now + 3600
            key = hashlib.sha256(token.encode()).hexdigest()
            if self.database:
                fields = {name: getattr(identity, name) for name in ('session_id', 'user_id', 'role')}
                with closing(private_connection(self.database.path)) as db, db:
                    db.execute('INSERT INTO web_sessions VALUES (?,?,?)',
                               (key, json.dumps(fields), time.time() + max(0, expires - now)))
            self._items[key] = WebSession(identity, expires)
            return token, max(1, int(expires - now))

    def require(self, token):
        if not token:
            raise PermissionError('会话已失效，请重新建立访客会话或登录。')
        key = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            session = self._items.get(key)
            if session is None or session.expires <= time.monotonic() or session.identity.revoked.is_set():
                self._remove(key)
                raise PermissionError('会话已失效，请重新建立访客会话或登录。')
            return session.identity

    def revoke(self, token):
        if token:
            with self._lock:
                self._remove(hashlib.sha256(token.encode()).hexdigest())


class RequestLimits:
    """本地接口的轻量入口限流，单进程有效，不信任客户端 X-Forwarded-For。"""
    def __init__(self):
        self._items = {}
        self._lock = RLock()

    def check(self, key, limit, window=60):
        with self._lock:
            now = time.monotonic()
            self._items = {k: v for k, v in self._items.items() if v[0] > now}
            expiry, count = self._items.get(key, (now + window, 0))
            if count >= limit or (key not in self._items and len(self._items) >= 5000):
                return False
            self._items[key] = (expiry, count + 1)
            return True
