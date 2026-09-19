#!/usr/bin/env python3
"""Manual/one-off poster for recipe_graph_stats() — the "state of the [sector]
graph" post (docs/social-posting-strategy.md Part 2 §3, Phase 1 item 3).

Deliberately NOT wired into bin/daily.py's backlog-drain machinery (that
loop's staging cache, channel-done tracking, and X confidence gate are all
built around one-post-per-card; a stats-based digest has no card and no
per-channel confidence signal, so bolting it on risked the one thing this
build is explicit about not doing — breaking daily.py's existing, well-tested
path). This script is the same shape as bin/matrix_poster.py's --event: small,
self-contained, safe to run repeatedly by hand while deciding wording/cadence,
promotable to its own channels.conf schedule row later the same way
graph-posters.md's --kind graph started manual before it got one.

Usage:
    python bin/graph_stats_post.py --tier arboryx.robotics                # preview
    python bin/graph_stats_post.py --tier arboryx.robotics --push --mode draft
    python bin/graph_stats_post.py --tier arboryx.robotics --push --mode now
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from bin._common import integration_ids_for, load_dotenv  # noqa: E402
from src.lib.channel_dispatch import channel_label, channel_parts  # noqa: E402
from src.lib.config_loader import load_tier  # noqa: E402
from src.lib.postiz_client import PostizClient  # noqa: E402
from src.lib.recipes import recipe_graph_stats  # noqa: E402
from src.lib.thread import max_chars_for_channel, split_for_thread  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", required=True, help="e.g. arboryx.robotics")
    ap.add_argument("--push", action="store_true", help="actually create in Postiz (default: preview only)")
    ap.add_argument("--mode", choices=["draft", "schedule", "now"], default="draft")
    args = ap.parse_args()

    load_dotenv()
    tier = load_tier(args.tier)

    try:
        bundle = recipe_graph_stats(tier)
    except KeyError as e:
        print(f"[graph_stats] nothing to post: {e}")
        return 0  # not an error — a stats-less week is expected sometimes

    iids = integration_ids_for(tier)
    if not iids:
        print(f"[graph_stats] no live channels for tier '{args.tier}'", file=sys.stderr)
        return 1

    print(f"source_id={bundle.source_id}\n")
    ec = None
    rendered: dict[str, list[str]] = {}
    for i in iids:
        lbl = channel_label(tier, i)
        ch_base_parts = split_for_thread(bundle.text, max_chars=max_chars_for_channel(lbl))
        ch_parts, ec = channel_parts(tier, lbl, source_type=bundle.source_type,
                                     source_id=bundle.source_id, parts=ch_base_parts,
                                     entities_cache=ec)
        rendered[lbl] = ch_parts
        print(f"--- [{lbl}] ---")
        for p in ch_parts:
            print(p)
            print()

    if not args.push:
        print("(preview only — pass --push to actually post)")
        return 0

    client = PostizClient(api_key=os.environ.get("POSTIZ_API_KEY"))
    for i in iids:
        lbl = channel_label(tier, i)
        try:
            resp = client.create_post(parts=rendered[lbl], integration_ids=[i], mode=args.mode)
            r = resp[0] if isinstance(resp, list) and resp else resp
            pid = (r or {}).get("id") or (r or {}).get("postId")
            print(f"[{lbl}] posted: {pid}")
        except Exception as e:  # noqa: BLE001
            print(f"[{lbl}] FAILED: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
