"""Resolve the right social handle for an entity, per channel.

Company handles differ by platform — Figure AI is @figure on LinkedIn but
@Figure_robots on X — so a post @-mentions the correct one for the channel it
publishes to. That means the TEXT diverges per channel (only the mentions).

Post-time resolution is STORE-ONLY and never guesses: a wrong @ tags the wrong
account, which is worse than no mention. The store (products/_shared/handles.json)
is curated/seeded and can be grown by `discover()` (LLM-assisted, verified
out-of-band by the validation team or a human) — that path is NOT used inline
at post time.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STORE = REPO_ROOT / "products" / "_shared" / "handles.json"

# channel aliases → canonical store key
_CHAN = {"x": "x", "twitter": "x", "linkedin": "linkedin", "li": "linkedin"}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _load() -> dict:
    try:
        return json.loads(STORE.read_text())
    except Exception:  # noqa: BLE001 — missing/unreadable store = no handles
        return {}


def handle_for(entity_name: str, channel: str) -> str | None:
    """The verified @handle for this entity on this channel, or None."""
    rec = _load().get(_norm(entity_name))
    if not isinstance(rec, dict):
        return None
    h = rec.get(_CHAN.get(channel.lower(), channel.lower()))
    return h or None


def apply_handles(text: str, entities: list | None, channel: str,
                  *, max_mentions: int = 2) -> str:
    """Append @handles for known entities on this channel (deduped vs the text,
    capped). Entities may be dicts ({name}) or plain strings. Unknown entities
    are silently skipped — we never invent a handle."""
    out = text.rstrip()
    low = out.lower()
    added = 0
    for e in (entities or []):
        if added >= max_mentions:
            break
        name = e.get("name") if isinstance(e, dict) else e
        h = handle_for(name or "", channel)
        if h and h.lower() not in low:
            out = f"{out} {h}"
            low = out.lower()
            added += 1
    return out
