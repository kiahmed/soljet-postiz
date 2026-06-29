#!/usr/bin/env python3
"""Compose and (optionally) push a post for one source item.

Usage:
  bin/draft.py --tier arboryx --source-id <id>                    # print only
  bin/draft.py --tier arboryx --source-id <id> --push             # create draft in Postiz
  bin/draft.py --tier arboryx --source-id <id> --push --mode now  # publish immediately
  bin/draft.py --tier arboryx --source-id <id> --push --mode schedule --in 2h
  bin/draft.py --tier arboryx --source-id <id> --push --mode schedule --at 2026-05-01T14:00:00Z

Tracking:
  Successful pushes are logged to data/posted_log.sqlite.
  By default, re-running for the same (source_id, tier) is a no-op.
  Use --force to override.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from _common import build_source, integration_ids_for, load_dotenv
from src.lib.composer import compose_catalyst, compose_finding
from src.lib.config_loader import load_tier
from src.lib.postiz_client import PostizClient
from src.lib.posted_log import is_posted, mark_posted


def _resolve_publish_date(args) -> str | None:
    if args.at:
        # accept naive ISO; assume UTC if no tz
        dt = datetime.fromisoformat(args.at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    if args.delay:
        m = re.fullmatch(r"(\d+)([dhm])", args.delay)
        if not m:
            raise SystemExit(f"--in must look like '2h' or '30m'; got {args.delay}")
        n, unit = int(m.group(1)), m.group(2)
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
        return (datetime.now(timezone.utc) + delta).isoformat()
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tier", required=True)
    p.add_argument("--source-id", required=True)
    p.add_argument("--source", help="firestore | duckdb | firestore_inherited")
    p.add_argument("--push", action="store_true")
    p.add_argument("--mode", default="draft", choices=["draft", "schedule", "now"])
    p.add_argument("--at", help="ISO timestamp for --mode schedule (UTC if no tz)")
    p.add_argument("--in", dest="delay", help="duration like 2h, 30m, 1d for --mode schedule")
    p.add_argument("--force", action="store_true", help="repost even if already in posted_log")
    args = p.parse_args()

    load_dotenv()
    tier = load_tier(args.tier)

    item = None
    related: list[dict] = []
    used_type = None
    for ds in tier.sources:
        if args.source and ds.type != args.source:
            continue
        try:
            src = build_source(ds, tier)
            item = src.get(args.source_id)
            related = src.get_related(args.source_id)
            used_type = ds.type
            break
        except KeyError:
            continue
    if not item:
        print(f"source-id '{args.source_id}' not found in tier '{tier.id}' sources", file=sys.stderr)
        return 1

    canonical_source = "firestore" if used_type in ("firestore", "firestore_inherited") else used_type

    if not args.force and args.push and is_posted(canonical_source, args.source_id, tier.id):
        print(f"already posted: {canonical_source}/{args.source_id} on tier {tier.id} (use --force to override)")
        return 0

    if used_type in ("firestore", "firestore_inherited"):
        text = compose_finding(tier, item)
    else:
        text = compose_catalyst(tier, item, related)

    print("---- DRAFT ----")
    print(text)
    print("---- /DRAFT ----")

    if not args.push:
        return 0

    iids = integration_ids_for(tier)
    if not iids:
        print("no integration IDs to push to (channels empty or all gated off)", file=sys.stderr)
        return 1

    publish_date = _resolve_publish_date(args)
    client = PostizClient(api_key=os.environ.get("POSTIZ_API_KEY"))
    res = client.create_post(
        text=text, integration_ids=iids, mode=args.mode, publish_date=publish_date
    )

    postiz_post_id = None
    if isinstance(res, list) and res:
        postiz_post_id = str(res[0].get("id") or "")
    elif isinstance(res, dict):
        postiz_post_id = str(res.get("id") or "")

    mark_posted(
        source_type=canonical_source,
        source_id=args.source_id,
        tier=tier.id,
        mode=args.mode,
        text=text,
        integration_ids=iids,
        postiz_post_id=postiz_post_id,
        response=res if isinstance(res, dict) else {"raw": res},
    )

    when = "as draft" if args.mode == "draft" else f"mode={args.mode} date={publish_date or 'now'}"
    print(f"pushed → {len(iids)} channel(s), {when}, postiz_post_id={postiz_post_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
