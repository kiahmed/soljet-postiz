#!/usr/bin/env python3
"""List recent items from a tier's data sources.

Usage:
  bin/list-recent.py --tier arboryx --since 1d
  bin/list-recent.py --tier arboryx --since 1d --unposted
  bin/list-recent.py --tier arboryx.robotics --since 7d
  bin/list-recent.py --tier arboryx.robotics --schema     # dump KG tables
"""
from __future__ import annotations

import argparse
import json
import sys

from _common import build_source, load_dotenv, parse_since
from src.lib.config_loader import load_tier
from src.lib.posted_log import posted_ids_for


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tier", required=True)
    p.add_argument("--since", default="1d", help="e.g. 1d, 24h, 90m, or ISO date")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--type", help="filter to one source type (firestore, duckdb, ...)")
    p.add_argument("--schema", action="store_true", help="dump duckdb table list and exit")
    p.add_argument("--unposted", action="store_true", help="hide items already in posted_log")
    p.add_argument("--json", action="store_true", help="emit JSON")
    args = p.parse_args()

    load_dotenv()
    tier = load_tier(args.tier)
    since = parse_since(args.since)

    if args.schema:
        for ds in tier.sources:
            if ds.type != "duckdb":
                continue
            src = build_source(ds, tier)
            print(f"# {ds.params.get('path')}")
            for t in src.tables():
                cols = src.schema(t)
                print(f"  {t}:")
                for c, ty in cols:
                    print(f"    - {c}: {ty}")
        return 0

    posted = posted_ids_for(tier.id) if args.unposted else set()

    all_items: list[dict] = []
    for ds in tier.sources:
        if args.type and ds.type != args.type:
            continue
        try:
            src = build_source(ds, tier)
            items = src.list_recent(since=since, limit=args.limit)
        except Exception as e:  # noqa: BLE001
            print(f"[{ds.type}] error: {e}", file=sys.stderr)
            continue
        for it in items:
            it["_source"] = ds.type
            it["_id"] = str(it.get("id") or it.get("catalyst_id") or it.get("_id") or "")
        all_items.extend(items)

    if args.unposted:
        all_items = [it for it in all_items if it["_id"] and it["_id"] not in posted]

    if args.json:
        print(json.dumps(all_items, default=str, indent=2))
        return 0

    for it in all_items[: args.limit]:
        date = it.get("timestamp") or it.get("date") or it.get("created_at") or ""
        cat = it.get("category") or ""
        title = (it.get("finding") or it.get("title") or it.get("name") or "")[:120]
        print(f"[{it['_source']}] {date} {cat:<20} {it['_id']}  {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
