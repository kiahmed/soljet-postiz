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
