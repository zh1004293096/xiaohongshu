"""SQLite 数据库操作封装"""
import sqlite3
import os
import threading
from typing import List, Optional
from data.models import Note, Comment

import config


class Database:
    """线程安全的 SQLite 数据库管理器"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.DB_PATH
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_tables()

    def _get_conn(self):
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_tables(self):
        """创建表结构"""
        with self._lock:
            conn = self._get_conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS notes (
                    note_id      TEXT PRIMARY KEY,
                    title        TEXT,
                    author_id    TEXT,
                    author_name  TEXT,
                    url          TEXT,
                    keyword      TEXT,
                    collected_at TEXT
                );

                CREATE TABLE IF NOT EXISTS comments (
                    comment_id   TEXT PRIMARY KEY,
                    note_id      TEXT,
                    content      TEXT,
                    user_id      TEXT,
                    user_name    TEXT,
                    created_at   TEXT,
                    note_title   TEXT,
                    note_url     TEXT,
                    keyword      TEXT,
                    is_intent    INTEGER DEFAULT 0,
                    collected_at TEXT
                );

                CREATE TABLE IF NOT EXISTS keywords (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword  TEXT UNIQUE,
                    added_at TEXT DEFAULT (datetime('now','localtime'))
                );

                CREATE INDEX IF NOT EXISTS idx_comments_note ON comments(note_id);
                CREATE INDEX IF NOT EXISTS idx_comments_intent ON comments(is_intent);
                CREATE INDEX IF NOT EXISTS idx_comments_keyword ON comments(keyword);
                CREATE INDEX IF NOT EXISTS idx_comments_date ON comments(created_at);
                CREATE INDEX IF NOT EXISTS idx_notes_keyword ON notes(keyword);
            """)
            conn.commit()
            conn.close()

    # ==================== 关键词管理 ====================

    def get_keywords(self) -> List[str]:
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute("SELECT keyword FROM keywords ORDER BY id").fetchall()
            conn.close()
            return [r['keyword'] for r in rows]

    def add_keyword(self, keyword: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("INSERT OR IGNORE INTO keywords (keyword) VALUES (?)", (keyword,))
                conn.commit()
                ok = conn.total_changes > 0
            finally:
                conn.close()
            return ok

    def remove_keyword(self, keyword: str):
        with self._lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM keywords WHERE keyword=?", (keyword,))
            conn.commit()
            conn.close()

    def init_default_keywords(self):
        """初始化预设关键词"""
        existing = self.get_keywords()
        for kw in config.DEFAULT_KEYWORDS:
            if kw not in existing:
                self.add_keyword(kw)

    # ==================== 笔记操作 ====================

    def save_note(self, note: Note) -> bool:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO notes VALUES (?,?,?,?,?,?,?)",
                    (note.note_id, note.title, note.author_id, note.author_name,
                     note.url, note.keyword, note.collected_at)
                )
                conn.commit()
            finally:
                conn.close()
        return True

    def note_exists(self, note_id: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT 1 FROM notes WHERE note_id=?", (note_id,)).fetchone()
            conn.close()
            return row is not None

    # ==================== 评论操作 ====================

    def save_comment(self, comment: Comment) -> bool:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO comments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (comment.comment_id, comment.note_id, comment.content,
                     comment.user_id, comment.user_name, comment.created_at,
                     comment.note_title, comment.note_url, comment.keyword,
                     1 if comment.is_intent else 0, comment.collected_at)
                )
                conn.commit()
            finally:
                conn.close()
        return True

    def comment_exists(self, comment_id: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT 1 FROM comments WHERE comment_id=?", (comment_id,)).fetchone()
            conn.close()
            return row is not None

    # ==================== 查询操作 ====================

    def get_results(
        self,
        keyword: str = None,
        start_date: str = None,
        end_date: str = None,
        intent_only: bool = False,
        limit: int = None,
        offset: int = 0,
    ) -> List[dict]:
        """查询评论结果，支持筛选"""
        conditions = []
        params = []

        if keyword:
            conditions.append("keyword = ?")
            params.append(keyword)
        if start_date:
            conditions.append("created_at >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("created_at <= ?")
            params.append(end_date)
        if intent_only:
            conditions.append("is_intent = 1")

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"SELECT * FROM comments {where} ORDER BY collected_at DESC"

        if limit:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])

        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(sql, params).fetchall()
            conn.close()
        return [dict(r) for r in rows]

    def get_statistics(self) -> dict:
        """获取统计信息"""
        with self._lock:
            conn = self._get_conn()
            total_notes = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            total_comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
            intent_comments = conn.execute("SELECT COUNT(*) FROM comments WHERE is_intent=1").fetchone()[0]
            keywords_used = conn.execute("SELECT DISTINCT keyword FROM comments").fetchall()
            conn.close()
        return {
            'total_notes': total_notes,
            'total_comments': total_comments,
            'intent_comments': intent_comments,
            'keywords_used': [k[0] for k in keywords_used],
        }

    # ==================== 清理操作 ====================

    def clear_all(self):
        with self._lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM comments")
            conn.execute("DELETE FROM notes")
            conn.commit()
            conn.close()

    def clear_by_keyword(self, keyword: str):
        with self._lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM comments WHERE keyword=?", (keyword,))
            conn.execute("DELETE FROM notes WHERE keyword=?", (keyword,))
            conn.commit()
            conn.close()
