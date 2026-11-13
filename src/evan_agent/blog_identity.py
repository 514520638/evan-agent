"""博客登录桥接：每次 HTTP 请求向 Java 校验 Cookie，不读取博客密码。"""

import hashlib
import os
import time
from threading import RLock

import httpx
from fastapi import HTTPException
from evan_agent.auth import AuthContext


class BlogIdentity:
    def __init__(self):
        self.base = os.getenv('BLOG_API_BASE_URL', 'https://evanlink.cn/api').rstrip('/')
        self._items = {}
        self._checked = {}
        self._lock = RLock()

    def resolve(self, cookie):
        """Cookie 只能发往由部署者配置的固定 Java 地址，错误时不授予身份。"""
        try:
            with httpx.Client(timeout=4, follow_redirects=False, trust_env=False) as client:
                response = client.get(self.base + '/admin/session', cookies={'evanlink_admin_token': cookie})
            if response.status_code == 401:
                self.revoke(cookie)
                raise HTTPException(401, '博客登录已过期，请重新登录。')
            if response.status_code != 200:
                raise HTTPException(503, '博客认证服务暂不可用，请稍后重试。')
            payload = response.json()
            remaining = float(payload['expires_at']) - time.time()
            if payload.get('role') != 'owner' or remaining <= 0:
                raise HTTPException(401, '博客登录已过期。')
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(503, '博客认证服务连接失败，请稍后重试。') from None
        key = hashlib.sha256(cookie.encode()).hexdigest()
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if v.is_owner}
            self._checked = {k: v for k, v in self._checked.items() if k in self._items}
            if key not in self._items:
                if len(self._items) >= 1000:
                    raise HTTPException(429, '登录会话过多，请稍后重试。')
                self._items[key] = AuthContext(session_id='blog:' + key, user_id='owner', role='owner',
                                               expires_at=time.monotonic() + remaining)
            self._checked[key] = time.monotonic()
            return self._items[key]

    def check_stream(self, cookie, identity):
        """流中持续检查撤销标记，每 15 秒重新检查 Java，避免逐 token 网络请求。"""
        key = hashlib.sha256(cookie.encode()).hexdigest()
        if not identity.is_owner:
            raise PermissionError('登录已退出或过期。')
        if time.monotonic() - self._checked.get(key, 0) > 15:
            self.resolve(cookie)

    def revoke(self, cookie):
        if not cookie:
            return
        key = hashlib.sha256(cookie.encode()).hexdigest()
        with self._lock:
            identity = self._items.pop(key, None)
            self._checked.pop(key, None)
            if identity:
                identity.revoked.set()

    def logout(self, cookie):
        if not cookie:
            return
        # 先让 Java 撤销登录，再清浏览器 Cookie；网络失败时不伪报两端已退出。
        try:
            with httpx.Client(timeout=4, follow_redirects=False, trust_env=False) as client:
                response = client.post(self.base + '/admin/logout', cookies={'evanlink_admin_token': cookie})
            if response.status_code != 200:
                raise HTTPException(503, '博客退出失败，请重试。')
        except httpx.HTTPError:
            raise HTTPException(503, '博客认证服务暂不可用，退出尚未完成。') from None
        self.revoke(cookie)
