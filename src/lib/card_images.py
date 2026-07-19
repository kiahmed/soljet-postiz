"""Which cards have a rendered PNG — the single source of truth for "ready".

The sibling KG renders one PNG per card into <KG_REPO>/data/exports/card_images/
(the same files its `make render-status` counts). A card without one still posts,
but on an `attach` channel (LinkedIn) it goes out with no image — so batch runs
skip un-rendered cards by default.

Used by bin/post-status.py (reporting) and bin/daily.py (--ready-only), so both
answer "is this card ready?" identically.
"""
from __future__ import annotations

import os
from pathlib import Path

from .config_loader import _TIER_DIR_BY_ID


def png_dir(tier) -> Path | None:
    """This tier's KG render dir, or None if it has no card-render pipeline.

    A tier with no KG_REPO_PATH (e.g. the parent, which posts entry links) has no
    card PNGs → None, ALWAYS, so it can never borrow another tier's renders via
    the override. KG_CARD_IMAGES_DIR only backs up a tier that HAS a pipeline but
    whose relative path doesn't resolve here (e.g. from a git worktree — the
    config's `../` assumes the main checkout)."""
    kg = getattr(tier, "raw", {}).get("KG_REPO_PATH")
    if not kg:
        return None
    base = _TIER_DIR_BY_ID.get(getattr(tier, "id", ""), Path.cwd())
    resolved = (base / kg / "data" / "exports" / "card_images").resolve()
    if resolved.is_dir():
        return resolved
    env = os.getenv("KG_CARD_IMAGES_DIR")
    return Path(env) if env else resolved   # nonexistent → treated as "no dir"


def has_render(tier, card_id: str) -> bool:
    """True if this card's PNG exists. False for tiers with no pipeline."""
    d = png_dir(tier)
    if not d or not d.is_dir() or not card_id:
        return False
    return (d / f"{card_id}.png").is_file()


def renders_available(tier) -> bool:
    """True if this tier has a card-render dir at all (so callers can tell
    'no pipeline' apart from 'pipeline exists, this card isn't rendered')."""
    d = png_dir(tier)
    return bool(d and d.is_dir())


def requires_render(tier) -> bool:
    """True if this tier must NOT post a card that has no rendered PNG.

    Keyed on the tier DECLARING a card pipeline (KG_REPO_PATH), not on the
    render dir existing — so if the KG mount is missing or the path doesn't
    resolve, we fail CLOSED (post nothing) instead of silently publishing
    imageless posts to an `attach` channel. Override per tier with
    REQUIRE_CARD_IMAGE="false" if you ever genuinely want text-only cards."""
    raw = getattr(tier, "raw", {}) or {}
    override = str(raw.get("REQUIRE_CARD_IMAGE", "")).strip().lower()
    if override in ("true", "1", "yes"):
        return True
    if override in ("false", "0", "no"):
        return False
    return bool(raw.get("KG_REPO_PATH"))   # declares a card pipeline → require it


def explain_missing(tier, card_id: str) -> str:
    """Human-readable reason a card is being skipped, so a missing mount is
    diagnosable instead of looking like an empty backlog."""
    d = png_dir(tier)
    if not d:
        return "tier declares no KG_REPO_PATH"
    if not d.is_dir():
        return f"render dir MISSING at {d} (KG repo not mounted?)"
    return f"no {card_id}.png in {d}"
