"""访客问题与城市级估算：定位后台执行，不保存精确地址，不随临时会话过期。"""
from contextlib import closing
from datetime import datetime, timezone
import ipaddress
import os
import time
from threading import Event, Thread

import httpx
from evan_agent.conversation_db import private_connection


class GuestQuestions:
    def __init__(self, path):
        self.path = path
        self.stop_event = Event()
        self.thread = None
        self.cache = {}
        with closing(private_connection(path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS guest_questions (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, question TEXT NOT NULL)')
            columns = {row[1] for row in db.execute('PRAGMA table_info(guest_questions)')}
            for name, definition in {'client_ip': 'TEXT', 'region': 'TEXT', 'geo_status': "TEXT NOT NULL DEFAULT 'unknown'"}.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE guest_questions ADD COLUMN {name} {definition}')
            db.execute('CREATE INDEX IF NOT EXISTS idx_guest_geo_pending ON guest_questions(geo_status,id)')
            db.execute('CREATE INDEX IF NOT EXISTS idx_guest_question_time ON guest_questions(created_at)')
            db.execute('CREATE TABLE IF NOT EXISTS geo_budget(day TEXT PRIMARY KEY, requests INTEGER NOT NULL)')

    def cleanup(self):
        with closing(private_connection(self.path)) as db, db:
            db.execute("DELETE FROM guest_questions WHERE created_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-183 days')")
            db.execute("UPDATE guest_questions SET client_ip=NULL,geo_status='unknown' WHERE geo_status='pending' AND created_at < strftime('%Y-%m-%dT%H:%M:%S','now','-1 day')")
            db.execute("DELETE FROM geo_budget WHERE day < date('now','-2 days')")

    def add(self, question, client_ip=None):
        try:
            address = ipaddress.ip_address(client_ip or '')
            public_ip = str(address) if address.is_global else None
        except ValueError:
            public_ip = None
        enabled = os.getenv('GUEST_GEO_ENABLED', 'true').lower() == 'true'
        status = 'pending' if public_ip and enabled else 'unknown'
        with closing(private_connection(self.path)) as db, db:
            db.execute('INSERT INTO guest_questions(created_at,question,client_ip,geo_status) VALUES (?,?,?,?)',
                       (datetime.now(timezone.utc).isoformat(), question[:12000], public_ip if status == 'pending' else None, status))
            db.execute('DELETE FROM guest_questions WHERE id NOT IN (SELECT id FROM guest_questions ORDER BY id DESC LIMIT 20000)')
        self.cleanup()

    def resolve_one(self):
        """仅向固定 HTTPS 服务发送公网 IP，不发送问题、Cookie 或身份信息。"""
        if os.getenv('GUEST_GEO_ENABLED', 'true').lower() != 'true':
            with closing(private_connection(self.path)) as db, db:
                db.execute("UPDATE guest_questions SET client_ip=NULL,geo_status='unknown' WHERE geo_status='pending'")
            return False
        with closing(private_connection(self.path)) as db:
            row = db.execute("SELECT id,client_ip FROM guest_questions WHERE geo_status='pending' ORDER BY id LIMIT 1").fetchone()
        if not row:
            return False
        key, ip = row
        region = None
        cached = self.cache.get(ip)
        if cached and cached[0] > time.monotonic():
            region = cached[1]
        else:
            day = datetime.now(timezone.utc).date().isoformat()
            with closing(private_connection(self.path)) as db, db:
                db.execute('INSERT OR IGNORE INTO geo_budget VALUES (?,0)', (day,))
                allowed = db.execute('UPDATE geo_budget SET requests=requests+1 WHERE day=? AND requests<900', (day,)).rowcount
            if allowed:
                try:
                    with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
                        result = client.get('https://ipwho.is/' + ip, params={'fields': 'success,country,region,city', 'lang': 'zh-CN'} )
                    body = result.json() if result.status_code == 200 else {}
                    if body.get('success') is True:
                        names = [str(body.get(field) or '').strip()[:100] for field in ('country', 'region', 'city')]
                        region = ' · '.join(dict.fromkeys(name for name in names if name)) or None
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    pass
            self.cache = {k: v for k, v in self.cache.items() if v[0] > time.monotonic()}
            if len(self.cache) >= 1024:
                self.cache.pop(next(iter(self.cache)))
            self.cache[ip] = (time.monotonic() + (86400 if region else 60), region)
        with closing(private_connection(self.path)) as db, db:
            # 成功或失败后都清除用于查询的原始 IP，只保留估算地区。
            db.execute('UPDATE guest_questions SET region=?,geo_status=?,client_ip=NULL WHERE id=?', (region, 'estimated' if region else 'unknown', key))
        return True

    def start(self):
        def run():
            while not self.stop_event.is_set():
                try:
                    worked = self.resolve_one()
                except Exception:
                    worked = False
                self.stop_event.wait(0.15 if worked else 2)
        self.thread = Thread(target=run, name='guest-region', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def list(self, offset=0, limit=30):
        with closing(private_connection(self.path)) as db:
            rows = db.execute('SELECT id,created_at,question,region,geo_status FROM guest_questions ORDER BY id DESC LIMIT ? OFFSET ?', (limit + 1, offset)).fetchall()
        return {'items': [dict(zip(('id', 'created_at', 'question', 'region', 'geo_status'), row)) for row in rows[:limit]], 'has_more': len(rows) > limit}
