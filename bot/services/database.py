import sqlite3
import os
from datetime import datetime
from config.settings import DB_PATH


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_history(user_id);

            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                remind_at TEXT,
                reminded INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_todo_user ON todos(user_id);

            CREATE TABLE IF NOT EXISTS yt_archive_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'both',
                status TEXT NOT NULL DEFAULT 'pending',
                result TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS yt_completed (
                video_id TEXT NOT NULL,
                task_type TEXT NOT NULL CHECK(task_type IN ('video', 'thumbnail')),
                title TEXT,
                author TEXT,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (video_id, task_type)
            );

            CREATE TABLE IF NOT EXISTS bookmarks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                url TEXT,
                tags TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_bookmark_user ON bookmarks(user_id);
        """)


def add_chat_message(user_id: int, role: str, content: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )


def get_chat_history(user_id: int, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def clear_chat_history(user_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))


def add_todo(user_id: int, content: str, remind_at: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO todos (user_id, content, remind_at) VALUES (?, ?, ?)",
            (user_id, content, remind_at),
        )
        return cur.lastrowid


def list_todos(user_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, content, completed, remind_at FROM todos WHERE user_id = ? AND completed = 0 ORDER BY id",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def complete_todo(user_id: int, todo_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE todos SET completed = 1 WHERE id = ? AND user_id = ? AND completed = 0",
            (todo_id, user_id),
        )
        return cur.rowcount > 0


def get_due_reminders() -> list[dict]:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, user_id, content FROM todos WHERE remind_at <= ? AND reminded = 0 AND completed = 0",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_reminded(todo_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE todos SET reminded = 1 WHERE id = ?", (todo_id,))


def add_yt_task(user_id: int, url: str, mode: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO yt_archive_tasks (user_id, url, mode) VALUES (?, ?, ?)",
            (user_id, url, mode),
        )
        return cur.lastrowid


def update_yt_task(task_id: int, status: str, result: str = ""):
    with get_conn() as conn:
        conn.execute(
            "UPDATE yt_archive_tasks SET status = ?, result = ? WHERE id = ?",
            (status, result, task_id),
        )


def get_yt_completed_ids() -> tuple[set, set]:
    video_ids, thumb_ids = set(), set()
    with get_conn() as conn:
        for row in conn.execute("SELECT video_id, task_type FROM yt_completed"):
            if row["task_type"] == "video":
                video_ids.add(row["video_id"])
            else:
                thumb_ids.add(row["video_id"])
    return video_ids, thumb_ids


def log_yt_completion(video_id: str, task_type: str, title: str, author: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO yt_completed (video_id, task_type, title, author, completed_at) VALUES (?, ?, ?, ?, ?)",
            (video_id, task_type, title, author, datetime.utcnow().isoformat()),
        )