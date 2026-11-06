"""本地单主人认证：密码校验由程序完成，身份不接受聊天文本赋值。

信任边界是本机操作系统账户：能修改程序或认证数据库的人拥有本机控制权。
本模块不提供 HTTP 登录或远程会话令牌。
"""

from contextlib import closing
from dataclasses import dataclass, field
from threading import Event
from getpass import GetPassWarning, getpass
import argparse
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
import warnings
from uuid import uuid4

# PBKDF2 使用独立随机盐；数据库不保存明文密码。
ITERATIONS = 600_000
SESSION_SECONDS = 30 * 60
MAX_FAILURES = 5
LOCK_SECONDS = 60
DEFAULT_DB = Path(__file__).resolve().parents[2] / ".local" / "auth.sqlite3"


@dataclass(frozen=True)
class AuthContext:
    """由认证入口生成的不可变身份，供 runtime.context 使用。

    session_id 表示登录／访客身份，每次身份切换都会更换。
    conversation_id 表示本次调用对应的聊天，由会话仓库设置。
    expires_at 是当前进程的单调时钟截止值，不序列化到数据库。
    """

    session_id: str
    user_id: str | None = None
    role: str = "guest"
    expires_at: float = 0.0
    conversation_id: str | None = None
    revoked: Event = field(default_factory=Event, compare=False, repr=False)

    @classmethod
    def guest(cls) -> "AuthContext":
        return cls(session_id=uuid4().hex)

    @property
    def is_owner(self) -> bool:
        return not self.revoked.is_set() and self.role == "owner" and self.user_id == "owner" and time.monotonic() < self.expires_at


def require_owner(context: AuthContext | None) -> str:
    """写入业务必须在执行前调用；拒绝访客、缺失身份及已过期的身份。

    未来工具中使用 require_owner(runtime.context)，不能让模型传入 role。
    返回可信的 owner ID，便于给知识记录设置 owner_id。
    """
    if not isinstance(context, AuthContext) or not context.is_owner:
        raise PermissionError("此操作需要本人登录，请先使用 /login。")
    return context.user_id


class AuthenticationError(Exception):
    """可以直接展示给用户的认证失败原因，不携带密码信息。"""


class AuthStore:
    """SQLite 持久化主人凭据与连续登录失败次数。"""

    def __init__(self, path: Path = DEFAULT_DB) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 创建时即限制文件权限，避免先创建可被其他用户读取的数据库。
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS owner (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                salt BLOB NOT NULL,
                digest BLOB NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0,
                locked_until REAL NOT NULL DEFAULT 0
            )""")

    def is_initialized(self) -> bool:
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute("SELECT 1 FROM owner WHERE id = 1").fetchone() is not None

    @staticmethod
    def _digest(password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)

    def initialize(self, password: str) -> None:
        """只允许创建一次主人密码，避免初始化命令覆盖既有身份。"""
        if len(password) < 6 or len(password) > 256 or not password.strip():
            raise AuthenticationError("密码须为 6–256 个字符，且不能全为空白。")
        salt = secrets.token_bytes(32)
        digest = self._digest(password, salt)
        try:
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("INSERT INTO owner (id, salt, digest) VALUES (1, ?, ?)", (salt, digest))
        except sqlite3.IntegrityError:
            raise AuthenticationError("主人账号已初始化，不能重复创建。") from None

    def login(self, password: str) -> AuthContext:
        """验证密码；失败计数落盘，五次失败后暂时锁定一分钟。"""
        failure = None
        # 用事务串行更新计数，多个终端不能通过同时请求丢失失败次数。
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT salt, digest, failures, locked_until FROM owner WHERE id=1").fetchone()
            if row is None:
                raise AuthenticationError("尚未设置主人密码，请先运行 uv run evan-auth init。")
            salt, expected, failures, locked_until = row
            now = time.time()
            if locked_until > now:
                raise AuthenticationError(f"登录暂时锁定，请 {int(locked_until - now) + 1} 秒后重试。")
            if locked_until:
                failures = 0
            valid = len(password) <= 256 and hmac.compare_digest(self._digest(password, salt), expected)
            if valid:
                db.execute("UPDATE owner SET failures=0, locked_until=0 WHERE id=1")
            else:
                failures += 1
                deadline = now + LOCK_SECONDS if failures >= MAX_FAILURES else 0
                db.execute("UPDATE owner SET failures=?, locked_until=? WHERE id=1", (failures, deadline))
                failure = "密码错误。" if not deadline else "连续五次密码错误，登录已锁定 60 秒。"
        # 必须在事务提交后抛错，否则失败计数会回滚。
        if failure:
            raise AuthenticationError(failure)
        return AuthContext(uuid4().hex, "owner", "owner", time.monotonic() + SESSION_SECONDS)


def read_password(prompt: str) -> str:
    """只接受终端隐藏输入，防止 getpass 降级后回显或读取聊天管道。"""
    if not sys.stdin.isatty():
        raise AuthenticationError("请在交互式终端中输入密码，不能通过管道或聊天消息登录。")
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        try:
            return getpass(prompt)
        except GetPassWarning:
            raise AuthenticationError("当前终端无法隐藏密码输入，请换一个交互式终端。") from None


def main() -> None:
    """独立初始化入口；不加载模型、不需要 API Key，也不把密码传给模型。"""
    parser = argparse.ArgumentParser(description="设置 Evan Agent 本人身份认证")
    parser.add_argument("action", choices=["init"], help="首次创建本机主人密码")
    parser.parse_args()
    try:
        store = AuthStore()
        if store.is_initialized():
            raise AuthenticationError("主人账号已初始化，不能重复创建。")
        password = read_password("设置主人密码（至少 6 个字符）：")
        if password != read_password("再次输入密码："):
            raise AuthenticationError("两次密码不一致，未保存。")
        store.initialize(password)
    except (AuthenticationError, EOFError, KeyboardInterrupt) as error:
        raise SystemExit(str(error) or "已取消初始化。") from None
    print("主人密码已设置。运行 uv run evan-agent 后输入 /login 登录。")


if __name__ == "__main__":
    main()
