import sqlite3
from pathlib import Path


DB_PATH = Path.home() / ".govcheck" / "govcheck.db"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            filename    TEXT NOT NULL,
            doc_type    TEXT NOT NULL,
            level       TEXT,
            content     TEXT,
            ingested_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS hierarchy (
            id          TEXT NOT NULL,
            parent_id   TEXT NOT NULL DEFAULT '',
            position    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (id, parent_id)
        );

        CREATE TABLE IF NOT EXISTS sections (
            section_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id      TEXT NOT NULL,
            heading     TEXT NOT NULL,
            level       INTEGER NOT NULL DEFAULT 1,
            content     TEXT,
            position    INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS audit_cache (
            cache_key   TEXT PRIMARY KEY,
            check_type  TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()


def upsert_document(conn: sqlite3.Connection, doc: dict) -> None:
    conn.execute("""
        INSERT INTO documents (id, title, filename, doc_type, level, content)
        VALUES (:id, :title, :filename, :doc_type, :level, :content)
        ON CONFLICT(id) DO UPDATE SET
            title      = excluded.title,
            filename   = excluded.filename,
            doc_type   = excluded.doc_type,
            level      = excluded.level,
            content    = excluded.content,
            ingested_at = datetime('now')
    """, doc)


def upsert_hierarchy_node(conn: sqlite3.Connection, node_id: str, parent_id: str | None, position: int) -> None:
    conn.execute("""
        INSERT INTO hierarchy (id, parent_id, position)
        VALUES (?, ?, ?)
        ON CONFLICT(id, parent_id) DO UPDATE SET
            position = excluded.position
    """, (node_id, parent_id or "", position))


def upsert_sections(conn: sqlite3.Connection, doc_id: str, sections: list[dict]) -> None:
    conn.execute("DELETE FROM sections WHERE doc_id = ?", (doc_id,))
    conn.executemany(
        "INSERT INTO sections (doc_id, heading, level, content, position) VALUES (?, ?, ?, ?, ?)",
        [(doc_id, s["heading"], s["level"], s["content"], s["position"]) for s in sections],
    )


def get_all_documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM documents").fetchall()


def get_hierarchy(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM hierarchy ORDER BY parent_id, position").fetchall()


def get_sections(conn: sqlite3.Connection, doc_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sections WHERE doc_id = ? ORDER BY position",
        (doc_id,),
    ).fetchall()


def get_document(conn: sqlite3.Connection, doc_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()


def find_section(conn: sqlite3.Connection, doc_id: str, query: str) -> sqlite3.Row | None:
    """Find a section by exact heading match, then by heading-prefix (case-insensitive)."""
    # exact match first
    row = conn.execute(
        "SELECT * FROM sections WHERE doc_id = ? AND heading = ? ORDER BY position LIMIT 1",
        (doc_id, query),
    ).fetchone()
    if row:
        return row
    # prefix match (e.g. "4.1" matches "4.1 Introduction")
    q = query.lower()
    rows = conn.execute(
        "SELECT * FROM sections WHERE doc_id = ? ORDER BY position",
        (doc_id,),
    ).fetchall()
    for row in rows:
        if row["heading"].lower().startswith(q):
            return row
    return None
