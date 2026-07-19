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

-- Posts handed to Postiz that hadn't reached a terminal state before the poll
-- timed out (state=QUEUE, typically dead Temporal pollers). They are IN FLIGHT,
-- not failed: once the workers come back, Temporal publishes them out of band.
-- Without this, the next run sees the item as unposted and posts it AGAIN —
-- which is exactly how CRY-031126-001 and ROB-031226-005 got duplicated on
-- LinkedIn. Reconciled at the start of the next run.
CREATE TABLE IF NOT EXISTS pending (
    tier        TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    source_type TEXT NOT NULL,
    post_id     TEXT NOT NULL,
    channel     TEXT,
    text        TEXT,
    integration_ids TEXT,
    queued_at   TEXT NOT NULL,
    PRIMARY KEY (tier, source_id, post_id)
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


def _channels_of(response_json: str | None) -> dict[str, str] | None:
    """{channel: state} from a stored response, or None when the row predates
    channel recording (treated as 'fully handled' so old rows never re-post)."""
    try:
        chans = (json.loads(response_json or "{}") or {}).get("channels")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(chans, list) or not chans:
        return None
    return {c.get("channel"): c.get("state") for c in chans
            if isinstance(c, dict) and c.get("channel")}


def published_channels(tier: str) -> dict[str, dict[str, str] | None]:
    """source_id -> {channel: state} for every recorded card in this tier.

    A card is only truly finished once EVERY channel we'd post it to has
    PUBLISHED. Without this the log is one row per card, so a card whose X post
    failed while LinkedIn succeeded looked 'done' and could never be completed."""
    with _conn() as c:
        rows = c.execute(
            "SELECT source_id, response FROM posted WHERE tier=?", (tier,)).fetchall()
    return {r[0]: _channels_of(r[1]) for r in rows}


def published_channels_for(tier: str, source_id: str) -> set[str]:
    """Channels that already PUBLISHED for this card — skip re-posting them."""
    with _conn() as c:
        row = c.execute("SELECT response FROM posted WHERE tier=? AND source_id=?",
                        (tier, source_id)).fetchone()
    st = _channels_of(row[0]) if row else None
    return {ch for ch, state in (st or {}).items() if state == "PUBLISHED"}


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
    # MERGE channel outcomes into any existing row: a retry that only posts the
    # previously-failed channel must not erase the channel that already worked.
    if response is not None:
        with _conn() as c:
            prev = c.execute("SELECT response FROM posted WHERE tier=? AND source_id=?",
                             (tier, source_id)).fetchone()
        old = _channels_of(prev[0]) if prev else None
        if old:
            merged = dict(old)
            for ch in (response.get("channels") or []):
                if isinstance(ch, dict) and ch.get("channel"):
                    merged[ch["channel"]] = ch.get("state")
            new_by_ch = {c.get("channel"): c for c in (response.get("channels") or [])
                         if isinstance(c, dict)}
            response = dict(response)
            response["channels"] = [
                new_by_ch.get(ch, {"channel": ch, "state": state})
                for ch, state in merged.items()]
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


# ------------------------------------------------------------------ in-flight
def add_pending(*, tier: str, source_id: str, source_type: str, post_id: str,
                channel: str = "", text: str = "", integration_ids=None) -> None:
    """Record a post left in QUEUE at poll timeout so the next run can reconcile
    it instead of re-posting the same item."""
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO pending (tier, source_id, source_type, "
            "post_id, channel, text, integration_ids, queued_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (tier, source_id, source_type, post_id, channel, text,
             json.dumps(list(integration_ids or [])),
             datetime.now(timezone.utc).isoformat()))


def all_pending(tier: str | None = None) -> list[dict]:
    sql = "SELECT * FROM pending"
    args: tuple = ()
    if tier:
        sql += " WHERE tier=?"
        args = (tier,)
    sql += " ORDER BY queued_at"
    with _conn() as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def clear_pending(tier: str, source_id: str, post_id: str | None = None) -> None:
    with _conn() as c:
        if post_id:
            c.execute("DELETE FROM pending WHERE tier=? AND source_id=? AND post_id=?",
                      (tier, source_id, post_id))
        else:
            c.execute("DELETE FROM pending WHERE tier=? AND source_id=?",
                      (tier, source_id))


def pending_ids_for(tier: str) -> set[str]:
    """source_ids still in flight for this tier — the picker must skip these so a
    queued-but-not-yet-published item is never posted twice."""
    with _conn() as c:
        rows = c.execute("SELECT source_id FROM pending WHERE tier=?", (tier,)).fetchall()
    return {r[0] for r in rows}


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
