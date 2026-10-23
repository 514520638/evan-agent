"""个人资料的本地持久化存储，不调用模型或生成向量。

数据库位于 .local/knowledge.sqlite3，与认证凭据分开。
所有写入在存储层检查本人身份，访客查询在 SQL 中过滤私人和未发布资料。
"""

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from evan_agent.auth import AuthContext, require_owner

DEFAULT_DB = Path(__file__).resolve().parents[2] / ".local" / "knowledge.sqlite3"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ProfileEntry:
    """数据库查询结果；内容保持录入原文，时间采用 UTC ISO 8601。"""

    id: str
    owner_id: str
    content: str
    category: str
    source: str
    visibility: str
    status: str
    created_at: str
    updated_at: str


class EntryNotFoundError(LookupError):
    """资料不存在或当前身份不可见，使用相同提示防止泄露私人记录。"""


class KnowledgeStore:
    """个人资料表与基础增删改查，后续 CLI、HTTP、工具共用此权限边界。"""

    def __init__(self, path: Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with closing(self._connect()) as db, db:
            # 版本检查和建表在同一事务内，避免两个进程同时初始化。
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise RuntimeError(f"不支持的个人资料数据库版本：{version}。请先升级程序或迁移数据库。")
            if version == 0:
                db.execute("""CREATE TABLE profile_entries (
                    id TEXT PRIMARY KEY NOT NULL,
                    owner_id TEXT NOT NULL,
                    content TEXT NOT NULL CHECK(length(trim(content)) > 0),
                    category TEXT NOT NULL CHECK(length(trim(category)) > 0),
                    source TEXT NOT NULL CHECK(source IN ('manual', 'conversation', 'import')),
                    visibility TEXT NOT NULL DEFAULT 'private'
                        CHECK(visibility IN ('private', 'public')),
                    status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft', 'published', 'archived')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""")
                db.execute("CREATE INDEX idx_profile_owner_updated ON profile_entries(owner_id, updated_at DESC, id)")
                db.execute("CREATE INDEX idx_profile_visibility ON profile_entries(owner_id, visibility, status, updated_at DESC, id)")
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _connect(self):
        """每次操作使用独立连接，退出作用域后关闭；参数化 SQL 防止注入。"""
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _validate(fields):
        """只校验不改写原文；枚举同时在 SQLite 中设约束。"""
        for key in ("content", "category"):
            if key in fields and (not isinstance(fields[key], str) or not fields[key].strip()):
                raise ValueError(f"{key} 必须是非空字符串。")
        for key, choices in {
            "source": ("manual", "conversation", "import"),
            "visibility": ("private", "public"),
            "status": ("draft", "published", "archived"),
        }.items():
            if key in fields and fields[key] not in choices:
                raise ValueError(f"{key} 必须是 {' / '.join(choices)}。")

    @staticmethod
    def _read_scope(identity: AuthContext):
        """在执行查询前确定权限，避免读取私人资料后再交给模型过滤。"""
        if not isinstance(identity, AuthContext):
            raise PermissionError("缺少可信身份。")
        if identity.revoked.is_set():
            raise PermissionError("当前登录会话已退出。")
        if identity.role == "owner":
            return "owner_id = ?", [require_owner(identity)]
        if identity.role != "guest" or identity.user_id is not None:
            raise PermissionError("无效身份。")
        # 当前应用只有一个主人，访客只可查看该主人的公开且已发布资料。
        return "owner_id = ? AND visibility = 'public' AND status = 'published'", ["owner"]

    def create(self, identity: AuthContext, *, content: str, category: str = "general",
               source: str = "manual", visibility: str = "private", status: str = "draft") -> ProfileEntry:
        """新增资料，默认私有草稿；owner_id 始终来自认证身份。"""
        fields = dict(content=content, category=category, source=source, visibility=visibility, status=status)
        self._validate(fields)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            owner_id = require_owner(identity)
            now = datetime.now(timezone.utc).isoformat()
            entry = ProfileEntry(uuid4().hex, owner_id, **fields, created_at=now, updated_at=now)
            db.execute("""INSERT INTO profile_entries
                (id, owner_id, content, category, source, visibility, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", tuple(vars(entry).values()))
        return entry

    def get(self, identity: AuthContext, entry_id: str) -> ProfileEntry:
        """读取一条允许访问的资料；私有 ID 对访客表现为不存在。"""
        clause, args = self._read_scope(identity)
        with closing(self._connect()) as db:
            row = db.execute(f"SELECT * FROM profile_entries WHERE id = ? AND {clause}", [entry_id, *args]).fetchone()
        if row is None:
            raise EntryNotFoundError("资料不存在或当前身份无权查看。")
        return ProfileEntry(**dict(row))

    def list_entries(self, identity: AuthContext, *, category: str | None = None,
                     limit: int = 50, offset: int = 0) -> list[ProfileEntry]:
        """分页读取可见资料，按更新时间倒序返回；不允许无限拉取。"""
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValueError("limit 须为 1–200 的整数，offset 须为非负整数。")
        clause, args = self._read_scope(identity)
        if category is not None:
            clause += " AND category = ?"
            args.append(category)
        with closing(self._connect()) as db:
            rows = db.execute(f"SELECT * FROM profile_entries WHERE {clause} ORDER BY updated_at DESC, id LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        return [ProfileEntry(**dict(row)) for row in rows]

    def search(self, identity: AuthContext, query: str, *, limit: int = 5, offset: int = 0) -> list[ProfileEntry]:
        """检索已发布资料：空格分隔关键词，命中任一词即返回。

        按命中关键词数量、更新时间、ID 排序。instr 使用字面子串匹配，
        两个中文字也可检索，% 和 _ 不会被解释为通配符。空查询列出资料。
        """
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("查询须为不超过 200 字符的字符串。")
        terms = list(dict.fromkeys(query.split()))
        if len(terms) > 8:
            raise ValueError("每次最多使用 8 个关键词，用空格分隔。")
        if type(limit) is not int or not 1 <= limit <= 11 or type(offset) is not int or offset < 0:
            raise ValueError("查询分页参数无效。")
        scope, scope_args = self._read_scope(identity)
        # 命中权重是简单计数，不是向量相似度；查询值始终参数化。
        expressions = ["(instr(lower(content), lower(?)) > 0 OR instr(lower(category), lower(?)) > 0)" for _ in terms]
        score = " + ".join(expressions) if terms else "0"
        term_args = [value for term in terms for value in (term, term)]
        with closing(self._connect()) as db:
            rows = db.execute(f"""SELECT * FROM (
                SELECT profile_entries.*, ({score}) AS match_score
                FROM profile_entries WHERE {scope} AND status = 'published'
            ) WHERE match_score >= ?
            ORDER BY match_score DESC, updated_at DESC, id LIMIT ? OFFSET ?""",
            [*term_args, *scope_args, 1 if terms else 0, limit, offset]).fetchall()
        return [ProfileEntry(**{key: row[key] for key in ProfileEntry.__dataclass_fields__}) for row in rows]

    def get_published(self, identity: AuthContext, entry_id: str) -> ProfileEntry:
        """模型详情工具只读取已发布且当前身份可见的资料。"""
        scope, args = self._read_scope(identity)
        with closing(self._connect()) as db:
            row = db.execute(f"SELECT * FROM profile_entries WHERE id = ? AND {scope} AND status = 'published'", [entry_id, *args]).fetchone()
        if row is None:
            raise EntryNotFoundError("未找到当前身份可查询的已发布资料。")
        return ProfileEntry(**dict(row))

    def update(self, identity: AuthContext, entry_id: str, **changes) -> ProfileEntry:
        """只更新允许编辑的字段，保持 ID、归属和创建时间不变。"""
        allowed = {"content", "category", "source", "visibility", "status"}
        if not changes or not changes.keys() <= allowed:
            raise ValueError("请提供可修改字段：content、category、source、visibility、status。")
        self._validate(changes)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            owner_id = require_owner(identity)
            changes["updated_at"] = datetime.now(timezone.utc).isoformat()
            # 字段名只来自上面的白名单，字段值通过参数绑定传入。
            assignments = ", ".join(f"{key} = ?" for key in changes)
            cursor = db.execute(f"UPDATE profile_entries SET {assignments} WHERE id = ? AND owner_id = ?",
                                [*changes.values(), entry_id, owner_id])
            if not cursor.rowcount:
                raise EntryNotFoundError("资料不存在或当前身份无权修改。")
            row = db.execute("SELECT * FROM profile_entries WHERE id = ? AND owner_id = ?", (entry_id, owner_id)).fetchone()
            return ProfileEntry(**dict(row))

    def delete(self, identity: AuthContext, entry_id: str) -> None:
        """永久删除本人资料；如需保留记录，可通过 update 将状态改为 archived。"""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            owner_id = require_owner(identity)
            cursor = db.execute("DELETE FROM profile_entries WHERE id = ? AND owner_id = ?", (entry_id, owner_id))
            if not cursor.rowcount:
                raise EntryNotFoundError("资料不存在或当前身份无权删除。")


def main():
    """只建立表结构，不创建示例资料或触发模型调用。"""
    parser = argparse.ArgumentParser(description="初始化本地个人资料数据库")
    parser.add_argument("action", choices=["init"])
    parser.parse_args()
    store = KnowledgeStore()
    print(f"个人资料数据库已就绪：{store.path}（结构版本 {SCHEMA_VERSION}）")


if __name__ == "__main__":
    main()
