"""HTTP 会话：浏览器只持有随机 HttpOnly Cookie，服务端保存身份。

单进程内存版，重启即失效；原始令牌不落盘，不能以聊天 ID 代替登录令牌。
"""

from dataclasses import dataclass
import hashlib
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

    def _remove(self, key):
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
            self._items[hashlib.sha256(token.encode()).hexdigest()] = WebSession(identity, expires)
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
