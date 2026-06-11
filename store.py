"""
SQLite store — the shared, normalized item schema (PRD §5) plus a tiny kv table
used to cache the last good /api/state payload as a fallback (PRD §2 freshness model).

Single local file, one user. No migrations framework needed at this size; the
schema is created with CREATE TABLE IF NOT EXISTS on first use.
"""

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "garden.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            TEXT NOT NULL,           -- stable per source item (e.g. gmail thread id)
    source        TEXT NOT NULL,           -- 'email' | 'messages' | 'news' | 'calendar'
    who           TEXT,                    -- sender / contact / news source / event title
    preview       TEXT,                    -- subject / snippet / headline
    ts            INTEGER,                 -- unix, last activity
    last_from_me  INTEGER,                 -- 1/0; null for news/calendar
    unread        INTEGER,                 -- 1/0; null where N/A
    extra         TEXT,                    -- json blob (event time, url, labels, …)
    updated_at    INTEGER,
    PRIMARY KEY (source, id)
);
CREATE INDEX IF NOT EXISTS idx_items_source_ts ON items(source, ts);

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at INTEGER
);

-- Distilled news headlines. Keyed by the stable article id so a successful
-- summary persists across RSS pulls (each article is summarized once, ever).
-- Lives separately from `items` precisely so replace_source('news') can't drop it.
CREATE TABLE IF NOT EXISTS news_distill (
    id         TEXT PRIMARY KEY,
    line       TEXT NOT NULL,
    created_at INTEGER
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


def replace_source(source: str, items: list[dict]) -> None:
    """Atomically swap in the freshly pulled items for one source.

    A read-only mirror of a live inbox is simplest to keep correct by replacing
    the whole source each pull (threads disappear when archived, etc.) rather than
    upserting and leaving stale rows behind.
    """
    now = int(time.time())
    rows = [
        (
            it["id"],
            source,
            it.get("who"),
            it.get("preview"),
            it.get("ts"),
            it.get("last_from_me"),
            it.get("unread"),
            json.dumps(it.get("extra")) if it.get("extra") is not None else None,
            now,
        )
        for it in items
    ]
    with _connect() as conn:
        conn.execute("DELETE FROM items WHERE source = ?", (source,))
        conn.executemany(
            "INSERT INTO items (id, source, who, preview, ts, last_from_me, unread, extra, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def source_updated_at(source: str) -> int | None:
    """Most recent updated_at across a source's rows, or None if it has none.
    Lets callers tell a freshly-pulled bed from one showing frozen/stale data."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(updated_at) AS mu FROM items WHERE source = ?", (source,)
        ).fetchone()
        return row["mu"] if row and row["mu"] is not None else None


def get_items(source: str) -> list[dict]:
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM items WHERE source = ? ORDER BY ts DESC", (source,)
        )
        out = []
        for r in cur.fetchall():
            d = dict(r)
            d["extra"] = json.loads(d["extra"]) if d["extra"] else None
            out.append(d)
        return out


def set_kv(key: str, value) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value), int(time.time())),
        )


def get_kv(key: str):
    with _connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None


# --- News distillation cache (summarize-once-ever) ---

def get_distilled(article_id: str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT line FROM news_distill WHERE id = ?", (article_id,)
        ).fetchone()
        return row["line"] if row else None


def set_distilled(article_id: str, line: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO news_distill (id, line, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET line = excluded.line",
            (article_id, line, int(time.time())),
        )


def get_distilled_map() -> dict[str, str]:
    with _connect() as conn:
        rows = conn.execute("SELECT id, line FROM news_distill").fetchall()
        return {r["id"]: r["line"] for r in rows}
