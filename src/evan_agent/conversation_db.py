"""SQLite 聊天存储：只序列化消息与必要身份字段，不保存密码或明文 Cookie。"""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

from langchain_core.messages import messages_from_dict, messages_to_dict

DEFAULT_SESSION_DB = Path(__file__).resolve().parents[2] / '.local' / 'conversations.sqlite3'


def private_connection(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    path.chmod(0o600)
    return sqlite3.connect(path, timeout=10, check_same_thread=False)


class ConversationDB:
    """单进程占用锁防止两个服务使用独立内存缓存覆盖同一个数据库。"""
    def __init__(self, path):
        import fcntl
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock)
            self._lock = None
            raise RuntimeError('聊天数据库已被其他进程占用，请先停止另一个 evan-api/evan-agent；目前只支持单进程。') from None
        with closing(private_connection(self.path)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, owner_json TEXT NOT NULL, messages_json TEXT NOT NULL,
                pending_json TEXT, operation_json TEXT, busy INTEGER NOT NULL DEFAULT 0
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS web_sessions (
                token_hash TEXT PRIMARY KEY, identity_json TEXT NOT NULL, expires_wall REAL NOT NULL
            )''')

    def load(self):
        with closing(private_connection(self.path)) as db:
            rows = db.execute('SELECT id,owner_json,messages_json,pending_json,operation_json,busy FROM conversations ORDER BY rowid').fetchall()
        return [(key, json.loads(owner), messages_from_dict(json.loads(messages)),
                 json.loads(pending) if pending else None, json.loads(operation) if operation else None, bool(busy))
                for key, owner, messages, pending, operation, busy in rows]

    def save(self, key, item):
        owner = {'session_id': item.owner.session_id, 'user_id': item.owner.user_id, 'role': item.owner.role}
        with closing(private_connection(self.path)) as db, db:
            db.execute('''INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET messages_json=excluded.messages_json,
                pending_json=excluded.pending_json,operation_json=excluded.operation_json,busy=excluded.busy''',
                (key, json.dumps(owner), json.dumps(messages_to_dict(item.messages), ensure_ascii=False),
                 json.dumps(item.pending, ensure_ascii=False) if item.pending else None,
                 json.dumps(item.operation, ensure_ascii=False) if item.operation else None, int(item.busy)))

    def delete(self, key):
        with closing(private_connection(self.path)) as db, db:
            db.execute('DELETE FROM conversations WHERE id=?', (key,))

    def close(self):
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None
