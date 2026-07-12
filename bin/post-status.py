#!/usr/bin/env python3
"""Posting status per tier: how many source catalysts are posted, and how many
have a rendered card PNG (the sibling KG's `render-status` counts the PNGs; this
crosses that with what we've actually posted).

  make post-status                 # every enabled tier
  make post-status TIER=arboryx.robotics
  make post-status TIER=arboryx.robotics MISSING=1   # also LIST the ids blocked
                                                     # on a missing PNG

Per tier it reports, over the catalysts in the tier's PRIMARY source:
  - posted / unposted           (from data/posted_log.sqlite)
  - PNG rendered (total)        (KG's data/exports/card_images/<id>.png)
  - unposted WITH png           = ready to post now
  - unposted MISSING png        = waiting on a KG render

PNG source: the KG repo's local render dir, resolved from each tier's
KG_REPO_PATH (override with env KG_CARD_IMAGES_DIR). A tier with no card-render
pipeline (e.g. the parent, which posts entry links) reports PNG as n/a.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from _common import build_source, load_dotenv  # noqa: E402
from src.lib.config_loader import _TIER_DIR_BY_ID, load_tier  # noqa: E402
from src.lib.posted_log import posted_ids_for  # noqa: E402

_SINCE = datetime(2015, 1, 1)   # "everything"
_LIMIT = 100000


def _png_dir(tier) -> Path | None:
    """KG render dir for this tier, or None if it has no card-render pipeline.

    A tier with no KG_REPO_PATH (e.g. the parent, which posts entry links) has no
    card PNGs → None, ALWAYS (the KG_CARD_IMAGES_DIR override never applies to it,
    so it can't wrongly borrow another tier's renders). The override only backs up
    a tier that HAS a pipeline but whose relative path doesn't resolve here (e.g.
    running from a git worktree — the config's `../` assumes the main checkout)."""
    kg = tier.raw.get("KG_REPO_PATH")
    if not kg:
        return None
    base = _TIER_DIR_BY_ID.get(tier.id, Path.cwd())
    resolved = (base / kg / "data" / "exports" / "card_images").resolve()
    if resolved.is_dir():
        return resolved
    env = os.getenv("KG_CARD_IMAGES_DIR")
    return Path(env) if env else resolved  # nonexistent path → reported as n/a


def _ids(tier) -> list[str]:
    if not tier.sources:
        return []
    src = build_source(tier.sources[0], tier)
    out = []
    for it in src.list_recent(since=_SINCE, limit=_LIMIT):
        sid = str(it.get("id") or it.get("catalyst_id")
                  or it.get("card_id") or it.get("_id") or "")
        if sid:
            out.append(sid)
    return out


def _row(label: str, value) -> None:
    print(f"    {label:<26} {value}")


def status_for(tier_id: str, *, show_missing: bool) -> None:
    try:
        tier = load_tier(tier_id)
    except Exception as e:  # noqa: BLE001
        print(f"[{tier_id}] load failed: {e}", file=sys.stderr)
        return
    print(f"\n[{tier.id}]  {tier.raw.get('TIER_NAME', '')}")
    try:
        ids = _ids(tier)
    except Exception as e:  # noqa: BLE001
        print(f"    source error: {e}", file=sys.stderr)
        return
    if not ids:
        _row("catalysts in source", 0)
        return

    posted = posted_ids_for(tier.id)
    pd = _png_dir(tier)
    has_pngs = bool(pd and pd.is_dir())

    def has_png(cid: str) -> bool:
        return has_pngs and (pd / f"{cid}.png").is_file()

    total = len(ids)
    n_posted = sum(1 for i in ids if i in posted)
    unposted = [i for i in ids if i not in posted]
    n_png = sum(1 for i in ids if has_png(i))
    unposted_ready = [i for i in unposted if has_png(i)]
    unposted_blocked = [i for i in unposted if not has_png(i)]

    _row("catalysts in source", total)
    _row("posted", n_posted)
    _row("unposted", len(unposted))
    if has_pngs:
        _row("PNG rendered (total)", n_png)
        _row("· unposted WITH png", f"{len(unposted_ready)}   (ready to post)")
        _row("· unposted MISSING png", f"{len(unposted_blocked)}   (waiting on KG render)")
    else:
        loc = pd if pd else "no KG_REPO_PATH"
        _row("PNG rendered", f"n/a — no card-render dir ({loc})")

    if show_missing:
        # ids blocked on a missing PNG (or, for a no-PNG tier, all unposted)
        blocked = unposted_blocked if has_pngs else unposted
        head = "unposted, MISSING png" if has_pngs else "unposted (tier has no card PNGs)"
        print(f"    {head}: {len(blocked)}")
        for cid in sorted(blocked)[:40]:
            print(f"      {cid}")
        if len(blocked) > 40:
            print(f"      … +{len(blocked) - 40} more")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tier", help="one tier id (default: all enabled)")
    p.add_argument("--missing", action="store_true",
                   help="list the ids blocked on a missing PNG")
    args = p.parse_args()
    load_dotenv()

    tiers = [args.tier] if args.tier else list(_TIER_DIR_BY_ID)
    for tid in tiers:
        status_for(tid, show_missing=args.missing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
