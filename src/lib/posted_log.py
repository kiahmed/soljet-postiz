"""Local SQLite log of which source items have been drafted/scheduled/posted.

Keyed on (source_type, source_id, tier). The same finding can be drafted
under different tiers (parent vs. branch); each is tracked separately.
Lives at data/posted_log.sqlite (gitignored)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "data" / "posted_log.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posted (
    source_type     TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    tier            TEXT NOT NULL,
    mode            TEXT NOT NULL,
    postiz_post_id  TEXT,
    posted_at       TEXT NOT NULL,
    integration_ids TEXT NOT NULL,
    text            TEXT NOT NULL,
    response        TEXT,
    PRIMARY KEY (source_type, source_id, tier)
);
"""


def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.executescript(_SCHEMA)
    return c


def is_posted(source_type: str, source_id: str, tier: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM posted WHERE source_type=? AND source_id=? AND tier=?",
            (source_type, source_id, tier),
        ).fetchone()
    return row is not None


def mark_posted(
    *,
    source_type: str,
    source_id: str,
    tier: str,
    mode: str,
    text: str,
    integration_ids: list[str],
    postiz_post_id: str | None = None,
    response: dict | None = None,
) -> None:
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO posted
               (source_type, source_id, tier, mode, postiz_post_id,
                posted_at, integration_ids, text, response)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_type,
                source_id,
                tier,
                mode,
                postiz_post_id,
                datetime.now(timezone.utc).isoformat(),
                ",".join(integration_ids),
                text,
                json.dumps(response) if response else None,
            ),
        )


def posted_ids_for(tier: str) -> set[str]:
    """All source_ids already published under this tier (any mode)."""
    with _conn() as c:
        rows = c.execute("SELECT source_id FROM posted WHERE tier=?", (tier,)).fetchall()
    return {r[0] for r in rows}


def all_entries(tier: str | None = None) -> list[dict]:
    sql = "SELECT * FROM posted"
    args: tuple = ()
    if tier:
        sql += " WHERE tier=?"
        args = (tier,)
    sql += " ORDER BY posted_at DESC"
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(sql, args).fetchall()
    return [dict(r) for r in rows]
