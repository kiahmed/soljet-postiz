#!/usr/bin/env python3
"""Inspect the local posted-log SQLite.

Usage:
  bin/posted-log.py                       # all entries
  bin/posted-log.py --tier arboryx        # filter by tier
  bin/posted-log.py --json
"""
from __future__ import annotations

import argparse
import json
import sys

from _common import load_dotenv
from src.lib.posted_log import all_entries


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tier")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    load_dotenv()
    rows = all_entries(args.tier)

    if args.json:
        print(json.dumps(rows, default=str, indent=2))
        return 0

    if not rows:
        print("(empty)")
        return 0
    for r in rows:
        print(f"{r['posted_at']:<28} [{r['mode']:<8}] {r['tier']:<20} {r['source_type']}/{r['source_id']}  postiz={r['postiz_post_id'] or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
