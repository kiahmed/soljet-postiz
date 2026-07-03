"""Per-post content cache — the exact text + media the poster will publish.

Keyed on (tier, source_id). The daily poster STAGES content here the first time
it generates a post (including during dry-run/preview), then REUSES it on the
actual push instead of re-composing. Two guarantees this buys:
  - What you preview is exactly what gets posted (no surprise LLM re-write).
  - Re-running never silently regenerates different text for the same item.

Pass --regenerate (or delete the file) to force a fresh compose. Lives under
data/content_cache/<tier>/<source_id>.json (gitignored — per-machine state)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "data" / "content_cache"


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def path_for(tier_id: str, source_id: str) -> Path:
    return CACHE_DIR / _safe(tier_id) / f"{_safe(source_id)}.json"


def load(tier_id: str, source_id: str) -> dict | None:
    """Return the staged content dict, or None if not cached / unreadable."""
    p = path_for(tier_id, source_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save(tier_id: str, source_id: str, *, source_type: str, text: str,
         parts: list[str], media_paths: list[str]) -> Path:
    p = path_for(tier_id, source_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "tier": tier_id,
        "source_id": source_id,
        "source_type": source_type,
        "text": text,
        "parts": parts,
        "media_paths": media_paths,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    return p
