"""Resolve the right social handle for an entity, per channel.

Company handles differ by platform — Figure AI is @figure-ai on LinkedIn but
@Figure_robot on X — so a post @-mentions the correct one for the channel it
publishes to (the only per-channel text delta).

`resolve_handle` is the single seam, with a priority chain so the handle SOURCE
can change by config alone, with no code change here:

  1. the handle embedded ON THE CARD ENTITY (linkedin_handle / x_handle) — the
     KG resolves once per canonical entity at ingestion and ships it on the card
     (recommended; cached forever, no runtime call).
  2. a configured resolver endpoint (tier.config HANDLE_ENDPOINT_URL) — POST the
     entity name, cache the reply (use when you want to backfill handles without
     regenerating cards).
  3. the local override store products/_shared/handles.json.
  4. None — never guess; a wrong @ tags the wrong company, so no-handle wins.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STORE = REPO_ROOT / "products" / "_shared" / "handles.json"

# channel aliases → canonical key ("x", "linkedin")
_CHAN = {"x": "x", "twitter": "x", "linkedin": "linkedin", "li": "linkedin"}

_ENDPOINT_CACHE: dict = {}  # (url, norm_name) -> {channel: handle}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _load() -> dict:
    try:
        return json.loads(STORE.read_text())
    except Exception:  # noqa: BLE001 — missing/unreadable store = no handles
        return {}


def handle_for(entity_name: str, channel: str) -> str | None:
    """Local-store lookup only (the manual override in handles.json)."""
    rec = _load().get(_norm(entity_name))
    if not isinstance(rec, dict):
        return None
    return rec.get(_CHAN.get(channel.lower(), channel.lower())) or None


def _endpoint_lookup(url: str, name: str, channel: str) -> str | None:
    key = (url, _norm(name))
    if key not in _ENDPOINT_CACHE:
        rec = {}
        try:
            import urllib.request
            body = json.dumps({"entities": [name],
                               "channels": ["linkedin", "x"]}).encode("utf-8")
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "User-Agent": "arboryx-daily/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            got = data.get(name) if isinstance(data, dict) else None
            rec = got if isinstance(got, dict) else {}
        except Exception:  # noqa: BLE001 — endpoint down = fall through, never fail a post
            rec = {}
        _ENDPOINT_CACHE[key] = rec
    return _ENDPOINT_CACHE[key].get(_CHAN.get(channel.lower(), channel.lower())) or None


def resolve_handle(entity, channel: str, *, tier=None) -> str | None:
    """The verified handle for this entity on this channel via the priority
    chain (card-embedded → endpoint → local store), or None. `entity` may be an
    entity dict (preferred — carries card-embedded handles) or a plain name."""
    ch = _CHAN.get(channel.lower(), channel.lower())
    if isinstance(entity, dict):
        embedded = entity.get(f"{ch}_handle")
        if embedded:
            return embedded
        name = entity.get("name")
    else:
        name = entity
    if not name:
        return None
    if tier is not None:
        url = tier.raw.get("HANDLE_ENDPOINT_URL")
        if url:
            h = _endpoint_lookup(url, name, channel)
            if h:
                return h
    return handle_for(name, channel)


def apply_handles(text: str, entities: list | None, channel: str,
                  *, max_mentions: int = 2, tier=None) -> str:
    """Append resolved @handles for entities on this channel (deduped, capped).
    Unknown entities are silently skipped — we never invent a handle."""
    out = text.rstrip()
    low = out.lower()
    added = 0
    for e in (entities or []):
        if added >= max_mentions:
            break
        h = resolve_handle(e, channel, tier=tier)
        if h and h.lower() not in low:
            out = f"{out} {h}"
            low = out.lower()
            added += 1
    return out
