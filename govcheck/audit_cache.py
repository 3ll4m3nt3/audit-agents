"""
Persistent cache for audit results keyed by document content hashes.

Prevents re-querying the Claude API when documents have not changed since the
last run. Results are stored in the `audit_cache` table (added to the same
SQLite database by `db.init_db`).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Optional


def compute_hash(content: str) -> str:
    """Return a SHA-256 hex digest of the given UTF-8 string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def get_cached(conn: sqlite3.Connection, cache_key: str) -> Optional[Any]:
    """Return the deserialized JSON value for *cache_key*, or None if not found."""
    row = conn.execute(
        "SELECT result_json FROM audit_cache WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    if row:
        return json.loads(row["result_json"])
    return None


def store_cached(
    conn: sqlite3.Connection,
    cache_key: str,
    check_type: str,
    value: Any,
) -> None:
    """Persist *value* (any JSON-serialisable object) under *cache_key*."""
    conn.execute(
        """
        INSERT INTO audit_cache (cache_key, check_type, result_json)
        VALUES (?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            result_json = excluded.result_json,
            created_at  = datetime('now')
        """,
        (cache_key, check_type, json.dumps(value)),
    )
    conn.commit()


def clear_cache(conn: sqlite3.Connection, check_type: Optional[str] = None) -> int:
    """Delete cached entries, optionally filtered to *check_type*. Returns row count."""
    if check_type:
        cursor = conn.execute(
            "DELETE FROM audit_cache WHERE check_type = ?", (check_type,)
        )
    else:
        cursor = conn.execute("DELETE FROM audit_cache")
    conn.commit()
    return cursor.rowcount
