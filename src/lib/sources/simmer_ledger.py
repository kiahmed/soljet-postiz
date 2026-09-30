"""Simmer event ledger — where the poster gets everything it posts.

EdgeLane's engine writes ONE row per postable event to Supabase
(`public.simmer_event_ledger`, EdgeLane migration 0019): the event attributes,
the data blocks the copy is written from, and the card's standalone HTML — all
frozen at the moment the engine detected the event. The Pub/Sub message only
wakes us; this row is the event.

So the poster no longer calls back into EdgeLane: no /simmer/state for the
numbers, no snap service loading /simmer/snap for the image (both rendered from
the readiness cache at FETCH time, so a post could show a later state than the
event it was about).

Simmer-specific vs Matrix (EdgeLane docs/simmer_events_update.md §3): the ledger
is EXACTLY-ONCE. The engine publishes only when its insert created a NEW row, and
`done` here does NOT delete — it keeps the event_id row as a payload-cleared
tombstone so a same-UTC-day re-fire can't re-publish. `prune` drops rows older
than 1 day (which outlives the UTC-day scope of the id).

Access is deliberately narrow: NOT the Supabase service key (that key can decrypt
users' broker tokens). We hold a ledger-only token and call four SECURITY DEFINER
functions that can touch nothing but this table:

    simmer_ledger_peek   read a row without taking it   (dry-run)
    simmer_ledger_claim  atomically take a pending row  (two workers can't both post it)
    simmer_ledger_done   payload-cleared tombstone once posted
    simmer_ledger_prune  drop rows older than 1 day     (keeps the ledger rolling)

Env: SUPABASE_URL, SUPABASE_ANON_KEY (the public/publishable key — the token is
what authorizes), SIMMER_LEDGER_TOKEN (Secret Manager `simmer-ledger-token`).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# An event older than this when we compose it is written in the past tense — a
# redelivered or delayed message must not claim a name "is ready" when it entered
# the band a while ago (EdgeLane docs/simmer_events_update.md §7).
PAST_AFTER_MIN = 10.0
# How far back prune reaches: 1 day, per §3/§6 — outlives the UTC-day id scope.
PRUNE_AGE = timedelta(days=1)


class LedgerClient:
    def __init__(self) -> None:
        self.url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
        self.anon = (os.getenv("SUPABASE_ANON_KEY") or "").strip()
        self.token = (os.getenv("SIMMER_LEDGER_TOKEN") or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.anon and self.token)

    def _rpc(self, fn: str, payload: dict, timeout: int = 15):
        body = json.dumps({"p_token": self.token, **payload}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/rest/v1/rpc/{fn}", data=body, method="POST",
            headers={"apikey": self.anon, "Authorization": f"Bearer {self.anon}",
                     "Content-Type": "application/json", "User-Agent": "simmer-poster/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore") or "null")

    def peek(self, event_id: str) -> dict | None:
        rows = self._rpc("simmer_ledger_peek", {"p_event_id": event_id}) or []
        return rows[0] if rows else None

    def claim(self, event_id: str, worker: str) -> dict | None:
        """The row, now ours — or None if it isn't there (already posted, pruned)
        or someone else holds it. A posted tombstone is never claimable."""
        rows = self._rpc("simmer_ledger_claim",
                         {"p_event_id": event_id, "p_worker": worker}) or []
        return rows[0] if rows else None

    def done(self, event_id: str) -> int:
        """Clears the payload and marks the row posted (a tombstone kept as the
        exactly-once dedupe key — see the module header)."""
        return int(self._rpc("simmer_ledger_done", {"p_event_id": event_id}) or 0)

    def prune_stale(self) -> int:
        """Drop rows older than 1 day. A claim held in the last 15 min is never
        pruned (the definer function enforces that)."""
        before = datetime.now(timezone.utc) - PRUNE_AGE
        return int(self._rpc("simmer_ledger_prune", {"p_before": before.isoformat()}) or 0)


def _parse_ts(v) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def card_from_row(row: dict, source_id: str) -> dict:
    """A ledger row → the card dict the Simmer recipes/imagery expect.

    Built directly from the frozen `data` blocks rather than reusing
    SimmerAPI._to_card: those blocks are the OUTPUT of EdgeLane's _state_block
    (the `card` block already projects `regime` to a string and carries no
    `metrics`), whereas _to_card expects the raw readiness envelope — feeding one
    to the other would misread the shape. The keys here match what compose_simmer
    / _simmer_bundle / _simmer_snap read; missing metrics degrade gracefully, the
    same as today's block=card callback path."""
    data = row.get("data") or {}
    attrs = row.get("attributes") or {}
    cb = data.get("card") or {}
    gates = data.get("gates") or {}
    score_block = data.get("score") or {}
    sym = str(row.get("symbol") or cb.get("symbol") or "").upper()
    state = str(row.get("state") or "")
    expiry = str(row.get("expiry") or cb.get("expiration") or "")[:10]
    computed = cb.get("computed_at") or ""
    veto = list(gates.get("veto_reasons") or [])

    event_at = _parse_ts(row.get("event_at"))
    age_min = ((datetime.now(timezone.utc) - event_at).total_seconds() / 60.0) if event_at else 0.0

    return {
        "card_id": source_id, "id": source_id, "symbol": sym, "state": state,
        "decision": cb.get("decision") or "",
        "expiry": expiry,
        "spot": cb.get("spot"),
        "score": cb.get("score"),
        "regime": cb.get("regime"),            # already a string in the card block
        "structure": cb.get("structure"),
        "strikes": cb.get("strikes"),
        "credit_mid": cb.get("credit_fill"),
        "gates": {"veto_reasons": veto, "failed": veto,
                  "clear": (not gates.get("vetoed")) if gates else (not veto),
                  "avoid_if": gates.get("avoid_if") or []},
        "sentiment": data.get("sentiment") or {},
        "components": score_block.get("components") or {},
        "evolution": (data.get("evolution") or {}).get("history") or [],
        "metrics": {},                          # card block carries none; compose degrades
        "date": str(computed)[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "computed_at": computed,
        "headline": f"${sym} — {'ready to serve' if state == 'ready' else 'started simmering'}",
        "entities": [{"name": sym, "x_handle": f"${sym}" if sym else None,
                      "linkedin_handle": None}],
        "url": f"https://simmer.facades.trade/?symbol={sym}",
        # ledger metadata + past-tense/catalyst flags the composer reads
        "event_id": row.get("event_id"),
        "event_at": row.get("event_at"),
        "_off_hours_catalyst": str(attrs.get("off_hours_catalyst") or "").lower() == "true",
        "_past": age_min > PAST_AFTER_MIN,
        "_snap_html": row.get("snap_html"),
        "_enrich": "ledger",
    }
