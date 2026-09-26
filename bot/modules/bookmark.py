"""Bookmark/memo module — save, list, search, and delete bookmarks."""

from __future__ import annotations

import json
import logging

from bot.services import database

logger = logging.getLogger(__name__)


def add_bookmark(user_id: int, title: str, content: str, url: str = "", tags: str = "") -> str:
    """Add a new bookmark."""
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO bookmarks (user_id, title, content, url, tags) VALUES (?, ?, ?, ?, ?)",
            (user_id, title, content, url or None, tags or None),
        )
        bookmark_id = cur.lastrowid
    return json.dumps({"status": "created", "bookmark_id": bookmark_id, "title": title})


def list_bookmarks(user_id: int, tag: str = "") -> str:
    """List all bookmarks, optionally filtered by tag."""
    with database.get_conn() as conn:
        if tag:
            rows = conn.execute(
                "SELECT id, title, content, url, tags, created_at FROM bookmarks WHERE user_id = ? AND tags LIKE ? ORDER BY id DESC",
                (user_id, f"%{tag}%"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, content, url, tags, created_at FROM bookmarks WHERE user_id = ? ORDER BY id DESC",
                (user_id,),
            ).fetchall()
    bookmarks = [dict(r) for r in rows]
    return json.dumps({"bookmarks": bookmarks, "count": len(bookmarks)})


def search_bookmarks(user_id: int, query: str) -> str:
    """Search bookmarks by title or content."""
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, content, url, tags, created_at FROM bookmarks "
            "WHERE user_id = ? AND (title LIKE ? OR content LIKE ?) ORDER BY id DESC",
            (user_id, f"%{query}%", f"%{query}%"),
        ).fetchall()
    bookmarks = [dict(r) for r in rows]
    return json.dumps({"bookmarks": bookmarks, "count": len(bookmarks), "query": query})


def delete_bookmark(user_id: int, bookmark_id: int) -> str:
    """Delete a bookmark by ID."""
    with database.get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM bookmarks WHERE id = ? AND user_id = ?",
            (bookmark_id, user_id),
        )
    if cur.rowcount > 0:
        return json.dumps({"status": "deleted", "bookmark_id": bookmark_id})
    return json.dumps({"status": "not_found", "bookmark_id": bookmark_id})