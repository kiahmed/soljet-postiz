#!/usr/bin/env python3
"""Posting status per tier: how many source catalysts are posted, and how many
have a rendered card PNG (the sibling KG's `render-status` counts the PNGs; this
crosses that with what we've actually posted).

  make post-status                 # every enabled tier
  make post-status TIER=arboryx.robotics
  make post-status TIER=arboryx.robotics MISSING=1   # also LIST the ids blocked
                                                     # on a missing PNG

It reports EVERY data source declared in the product's config (source 1 is
flagged as primary — the one the poster actually pulls from), and per source:
  - posted / unposted           (from data/posted_log.sqlite)
  - PNG rendered                (KG's data/exports/card_images/<id>.png)
  - unposted ready / blocked    (has png = ready; missing png = waiting on render)
plus a combined unique-id total across sources.

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


def _ids_for(ds, tier) -> list[str]:
    src = build_source(ds, tier)
    out = []
    for it in src.list_recent(since=_SINCE, limit=_LIMIT):
        sid = str(it.get("id") or it.get("catalyst_id")
                  or it.get("card_id") or it.get("_id") or "")
        if sid:
            out.append(sid)
    return out


def _src_label(ds) -> str:
    p = getattr(ds, "params", None) or {}
    ident = p.get("collection") or p.get("path") or p.get("inherit_from") or ""
    return f"{ds.type}" + (f" ({ident})" if ident else "")


def _row(label: str, value) -> None:
    print(f"    {label:<26} {value}")


def status_for(tier_id: str, *, show_missing: bool) -> None:
    try:
        tier = load_tier(tier_id)
    except Exception as e:  # noqa: BLE001
        print(f"[{tier_id}] load failed: {e}", file=sys.stderr)
        return
    print(f"\n[{tier.id}]  {tier.raw.get('TIER_NAME', '')}")
    if not tier.sources:
        print("    (no data sources configured)")
        return

    posted = posted_ids_for(tier.id)
    pd = _png_dir(tier)
    has_pngs = bool(pd and pd.is_dir())

    def has_png(cid: str) -> bool:
        return has_pngs and (pd / f"{cid}.png").is_file()

    seen: set[str] = set()   # for the combined-unique total across sources
    # Report EVERY source declared in the product config, not just the primary.
    for n, ds in enumerate(tier.sources, 1):
        tag = "  [primary — poster uses this]" if n == 1 else ""
        print(f"  source {n}: {_src_label(ds)}{tag}")
        try:
            ids = _ids_for(ds, tier)
        except Exception as e:  # noqa: BLE001 — one bad source shouldn't hide the rest
            print(f"    source error: {e}")
            continue
        seen.update(ids)
        if not ids:
            _row("catalysts", 0)
            continue

        unposted = [i for i in ids if i not in posted]
        _row("catalysts", len(ids))
        _row("posted", sum(1 for i in ids if i in posted))
        _row("unposted", len(unposted))
        if has_pngs:
            ready = [i for i in unposted if has_png(i)]
            blocked = [i for i in unposted if not has_png(i)]
            _row("PNG rendered", sum(1 for i in ids if has_png(i)))
            _row("· unposted ready", f"{len(ready)}   (has png)")
            _row("· unposted blocked", f"{len(blocked)}   (waiting on KG render)")
        else:
            blocked = unposted
            _row("PNG rendered", "n/a — no card-render pipeline for this tier")
        if show_missing and blocked:
            head = "unposted, MISSING png" if has_pngs else "unposted (no card PNGs)"
            print(f"      {head}: {len(blocked)}")
            for cid in sorted(blocked)[:40]:
                print(f"        {cid}")
            if len(blocked) > 40:
                print(f"        … +{len(blocked) - 40} more")

    if len(tier.sources) > 1:
        _row("combined (unique ids)", f"{len(seen)}   "
             f"({sum(1 for i in seen if i in posted)} posted)")


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
