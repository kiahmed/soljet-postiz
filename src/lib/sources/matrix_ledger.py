"""Matrix event ledger — where the poster gets everything it posts.

EdgeLane's engine writes ONE row per postable event to Supabase
(`public.matrix_event_ledger`, EdgeLane migration 0017): the event attributes,
the data blocks the copy is written from, and the card's standalone HTML — all
frozen at the moment the engine detected the event. The Pub/Sub message only
wakes us; this row is the event.

So the poster no longer calls back into EdgeLane at all: no /matrix/state for
the numbers, no snap service loading /matrix/snap for the image (both rendered
from the snapshot at FETCH time, so a post could show a later state than the
event it was about).

Access is deliberately narrow: NOT the Supabase service key (that key can
decrypt users' broker tokens). We hold a ledger-only token and call four
SECURITY DEFINER functions that can touch nothing but this table:

    matrix_ledger_peek   read a row without taking it   (dry-run)
    matrix_ledger_claim  atomically take a pending row  (two workers can't both post it)
    matrix_ledger_done   delete it once posted
    matrix_ledger_prune  drop stale rows                (keeps the ledger rolling)

Env: SUPABASE_URL, SUPABASE_ANON_KEY (the public/publishable key — the token is
what authorizes), MATRIX_LEDGER_TOKEN (Secret Manager `matrix-ledger-token`).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .matrix_source import MatrixAPI

_ET = ZoneInfo("America/New_York")

# An event older than this when we compose it is written in the past tense —
# a redelivered or delayed message must not claim something is happening "now".
PAST_AFTER_MIN = 10.0
# These describe something that has already ended, whenever they're posted.
ALWAYS_PAST = {"pick_result", "daily_recap"}


class LedgerClient:
    def __init__(self) -> None:
        self.url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
        self.anon = (os.getenv("SUPABASE_ANON_KEY") or "").strip()
        self.token = (os.getenv("MATRIX_LEDGER_TOKEN") or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.anon and self.token)

    def _rpc(self, fn: str, payload: dict, timeout: int = 15):
        body = json.dumps({"p_token": self.token, **payload}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/rest/v1/rpc/{fn}", data=body, method="POST",
            headers={"apikey": self.anon, "Authorization": f"Bearer {self.anon}",
                     "Content-Type": "application/json", "User-Agent": "matrix-poster/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore") or "null")

    def peek(self, event_id: str) -> dict | None:
        rows = self._rpc("matrix_ledger_peek", {"p_event_id": event_id}) or []
        return rows[0] if rows else None

    def claim(self, event_id: str, worker: str) -> dict | None:
        """The row, now ours — or None if it isn't there (already posted,
        pruned) or someone else holds it."""
        rows = self._rpc("matrix_ledger_claim",
                         {"p_event_id": event_id, "p_worker": worker}) or []
        return rows[0] if rows else None

    def done(self, event_id: str) -> int:
        return int(self._rpc("matrix_ledger_done", {"p_event_id": event_id}) or 0)

    def prune_before_today(self) -> int:
        """Matrix posts are about TODAY's session: drop anything from before the
        start of today (ET). A claim held in the last 15 min is never pruned."""
        start = datetime.now(_ET).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(self._rpc("matrix_ledger_prune",
                             {"p_before": start.astimezone(timezone.utc).isoformat()}) or 0)


def _parse_ts(v) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def card_from_row(row: dict, source_id: str) -> dict:
    """A ledger row → the card dict compose_matrix()/imagery expect. The row's
    `data` blocks are the exact shapes EdgeLane's /matrix/state returned, so the
    same mapping (MatrixAPI._to_card) is reused rather than restated."""
    data = row.get("data") or {}
    attrs = row.get("attributes") or {}
    sym = str(row.get("symbol") or "").upper()
    state = str(row.get("state") or "")

    card = MatrixAPI._to_card({"symbol": sym, "data": data.get("pick") or {}})
    for block in ("bias", "win_eval", "walls"):
        card[block] = data.get(block) or {}
    card["grid"] = (data.get("grid") or {}).get("grid") or {}

    trust = (card.get("bias") or {}).get("trust") or {}
    card["hint_text"] = trust.get("hint_text") if trust.get("show_hint") else None
    card["bias_trust_state"] = trust.get("state")
    card["win_rate"] = trust.get("win_rate")
    card["graded"] = trust.get("graded")

    if state == "pick_result":
        for k in ("result", "entry_premium", "exit_premium", "favorable_delta", "held_minutes"):
            card[k] = attrs.get(k)
    if state == "daily_recap":
        card["session_date"] = attrs.get("session_date")

    event_at = _parse_ts(row.get("event_at"))
    age_min = ((datetime.now(timezone.utc) - event_at).total_seconds() / 60.0) if event_at else 0.0
    card.update({
        "card_id": source_id, "id": source_id, "state": state,
        "expiry": row.get("expiry") or card.get("expiry") or "",
        "event_id": row.get("event_id"),
        "event_at": row.get("event_at"),
        "_past": state in ALWAYS_PAST or age_min > PAST_AFTER_MIN,
        "_snap_html": row.get("snap_html"),
        "_snap_view": row.get("snap_view"),
        "_enrich": "ledger",
    })
    return card
