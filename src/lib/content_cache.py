"""Per-post content cache — the exact text + media the poster will publish.

Keyed on (tier, source_id). The daily poster STAGES content here the first time
it generates a post (including during dry-run/preview), then REUSES it on the
actual push instead of re-composing. Two guarantees this buys:
  - What you preview is exactly what gets posted (no surprise LLM re-write).
  - Re-running never silently regenerates different text for the same item.

Pass --regenerate (or delete the file) to force a fresh compose. Lives under
data/content_cache/<tier>/<source_id>.json (gitignored — per-machine state).

Staged entries carry a COMPOSER FINGERPRINT (a hash of the modules that produce
the text). When composition logic changes, older entries no longer match and are
treated as absent, so they re-compose instead of silently serving stale copy —
that bit us once: a card staged before the relationship-hook change kept
replaying the old "How it's played out since" hook, and the only tell was
eyeballing the post."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "data" / "content_cache"

# Modules whose contents determine the staged text. recipes.py picks the recipe
# and the char budget; composer.py writes the copy (body, hashtags, temporal
# lead-in, relationship hook). Per-channel bits (@handles, $cashtags, imagery)
# are applied AFTER staging by channel_dispatch, so they don't belong here.
_FINGERPRINT_SOURCES = ("composer.py", "recipes.py")
_fp_cache: str | None = None


def _fingerprint() -> str:
    """Short hash of the composition modules; memoized per process."""
    global _fp_cache
    if _fp_cache is None:
        h = hashlib.sha256()
        for name in _FINGERPRINT_SOURCES:          # fixed order → stable hash
            try:
                h.update((Path(__file__).parent / name).read_bytes())
            except OSError:
                h.update(b"?")                     # missing file still hashes
        _fp_cache = h.hexdigest()[:12]
    return _fp_cache


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def path_for(tier_id: str, source_id: str) -> Path:
    return CACHE_DIR / _safe(tier_id) / f"{_safe(source_id)}.json"


def load(tier_id: str, source_id: str) -> dict | None:
    """Return the staged content dict, or None if not cached / unreadable /
    composed by a different version of the composer (stale → re-compose)."""
    p = path_for(tier_id, source_id)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if d.get("composer_fp") != _fingerprint():
        # Composition logic moved on (or the entry predates fingerprinting).
        # Treat as absent so the caller re-composes and re-stages.
        print(f"    [staged] {source_id}: composer changed since it was staged "
              f"— re-composing", file=sys.stderr)
        return None
    return d


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
        "composer_fp": _fingerprint(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    return p


def delete(tier_id: str, source_id: str) -> None:
    """Remove a staged entry after it's been successfully posted (the posted-log
    is the record now; the staged JSON is redundant). No-op if absent."""
    try:
        path_for(tier_id, source_id).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
