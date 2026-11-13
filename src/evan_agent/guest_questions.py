"""访客问题独立存储：不包含 Cookie/IP，不随临时聊天过期删除。"""
from contextlib import closing
from datetime import datetime, timezone
from evan_agent.conversation_db import private_connection


class GuestQuestions:
    def __init__(self, path):
        self.path = path
        with closing(private_connection(path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS guest_questions (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, question TEXT NOT NULL)')

    def cleanup(self):
        with closing(private_connection(self.path)) as db, db:
            db.execute("DELETE FROM guest_questions WHERE created_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-183 days')")

    def add(self, question):
        with closing(private_connection(self.path)) as db, db:
            db.execute('INSERT INTO guest_questions(created_at,question) VALUES (?,?)',
                       (datetime.now(timezone.utc).isoformat(), question[:12000]))
            # 半年自动轮换，并限制条数，避免无限增长。
            db.execute("DELETE FROM guest_questions WHERE created_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-183 days')")
            db.execute('DELETE FROM guest_questions WHERE id NOT IN (SELECT id FROM guest_questions ORDER BY id DESC LIMIT 20000)')

    def list(self, offset=0, limit=30):
        with closing(private_connection(self.path)) as db:
            rows = db.execute('SELECT id,created_at,question FROM guest_questions ORDER BY id DESC LIMIT ? OFFSET ?', (limit + 1, offset)).fetchall()
        return {'items': [dict(zip(('id', 'created_at', 'question'), row)) for row in rows[:limit]], 'has_more': len(rows) > limit}
