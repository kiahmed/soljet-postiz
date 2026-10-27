#!/usr/bin/env python3
"""Weekly "what moved in the sector" digest — recipe_graph_stats(): the week's
strongest graph_insights[] (up to 2 hub entities + 1 sector-wide trend),
headlines quoted verbatim, topical hashtags, link to the sector site
(docs/social-posting-strategy.md Part 2 §3).

Runs as the scheduler's `insights` kind (ops/scheduler/channels.conf ->
run-daily.sh -> `make insights-post`), one row per channel, weekly. Kept out
of bin/daily.py's backlog-drain loop on purpose: that loop's staging cache,
channel-done tracking and X confidence gate are all built around
one-post-per-card, and a sector digest has no card.

Repeat protection lives in posted_log (tier key "<tier>:insights:<channel>"):
each published digest records the subjects it featured, and a subject isn't
re-featured for 21 days. A week with <2 fresh subjects posts nothing.

Usage:
    python bin/graph_stats_post.py --tier arboryx.robotics                       # preview
    python bin/graph_stats_post.py --tier arboryx.robotics --channel linkedin --push --mode now
    python bin/graph_stats_post.py --tier arboryx.robotics --push --mode draft   # Postiz drafts
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "bin"))

import daily  # noqa: E402  (poll_publish, _kind_channel_enabled)
from bin._common import integration_ids_for, load_dotenv  # noqa: E402
from src.lib import posted_log  # noqa: E402
from src.lib import channel_dispatch  # noqa: E402
from src.lib.channel_dispatch import channel_label, channel_parts  # noqa: E402
from src.lib.config_loader import load_tier  # noqa: E402
from src.lib.postiz_client import PostizClient  # noqa: E402
from src.lib.recipes import insights_tkey, recipe_graph_stats  # noqa: E402
from src.lib.thread import max_chars_for_channel, split_for_thread  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", required=True, help="e.g. arboryx.robotics")
    ap.add_argument("--channel", help="one channel only (linkedin | x) — how the scheduler calls it")
    ap.add_argument("--push", action="store_true", help="actually create in Postiz (default: preview only)")
    ap.add_argument("--mode", choices=["draft", "schedule", "now"], default="draft")
    args = ap.parse_args()

    load_dotenv()
    tier = load_tier(args.tier)

    iids = integration_ids_for(tier)
    if args.channel:
        iids = [i for i in iids if channel_label(tier, i).lower() == args.channel.lower()]
    # INSIGHTS_POST_<CHANNEL>_ENABLED="false" in tier.config pulls one channel
    # without touching the Cloud Scheduler job (same switch pattern as graph).
    iids = [i for i in iids if daily._kind_channel_enabled(tier, "insights", channel_label(tier, i))]
    if not iids:
        print(f"[graph_stats] no live/enabled channels for tier '{args.tier}'"
              f"{' / ' + args.channel if args.channel else ''} — skip")
        return 0

    client = PostizClient(api_key=os.environ.get("POSTIZ_API_KEY")) if args.push else None
    ec = None  # unused: entities come from each bundle
    for iid in iids:
        lbl = channel_label(tier, iid)
        try:
            bundle = recipe_graph_stats(tier, lbl)
        except KeyError as e:
            print(f"[graph_stats] [{lbl}] nothing to post: {e}")
            continue  # not an error — a quiet week is expected sometimes
        keys = bundle.context.get("insight_keys", [])

        base = split_for_thread(bundle.text, max_chars=max_chars_for_channel(lbl))
        # Digest entities get @handles / LinkedIn mentions through the same path
        # as card posts (HANDLE_INJECTION, resolver chain, URN guard, per-tag fit).
        # The cleaned headlines stay mention-free; tags go only on the hashtag line.
        channel_dispatch._ctx_cache[bundle.source_id] = bundle.context.get("tag_context", "")
        parts, ec = channel_parts(tier, lbl, source_type=bundle.source_type,
                                  source_id=bundle.source_id, parts=base,
                                  entities_cache=bundle.context.get("entities") or [])
        print(f"--- [{lbl}] {bundle.source_id} ---")
        for p in parts:
            print(p)
            print()
        if not args.push:
            print("(preview only — pass --push to actually post)")
            continue

        # Claim the week BEFORE posting. Cloud Scheduler is at-least-once and the
        # poll below can take ~2 min, so recording only afterwards leaves a window
        # where a duplicate delivery would pass the same-week guard in
        # recipe_graph_stats and post a second digest. The claim carries the same
        # "keys", so the 21-day cooldown is identical either way. Released below
        # if nothing was accepted or the post confirmed-ERRORed. Draft/schedule
        # runs never claim (a draft must not block the real weekly post).
        tkey = insights_tkey(tier.id, lbl)
        claimed = False
        if args.mode == "now":
            try:
                posted_log.mark_posted(
                    source_type=bundle.source_type, source_id=bundle.source_id,
                    tier=tkey, mode="now", text=bundle.text, integration_ids=[iid],
                    response={"channels": [{"channel": lbl, "state": "CLAIMED"}], "keys": keys})
                claimed = True
            except Exception as e:  # noqa: BLE001 — can't claim -> don't post unclaimed
                print(f"[{lbl}] could not claim the week, not posting: {e}", file=sys.stderr)
                continue

        try:
            resp = client.create_post(parts=parts, integration_ids=[iid], mode=args.mode)
            r = resp[0] if isinstance(resp, list) and resp else resp
            pid = (r or {}).get("postId") or (r or {}).get("id")
        except Exception as e:  # noqa: BLE001 — one channel's failure can't block the other
            print(f"[{lbl}] FAILED: {e}", file=sys.stderr)
            if claimed:  # never accepted -> release the claim
                posted_log.unmark_posted(tkey, bundle.source_id)
            continue
        if args.mode != "now":
            print(f"[{lbl}] created ({args.mode}): {pid}")
            continue
        state, url, err = daily.poll_publish(pid)
        print(f"[{lbl}] {state} {url or err}")
        # Record on PUBLISHED, and also on QUEUE (Postiz accepted it, poll just
        # timed out): that one may still publish later, and not recording it
        # would let next week re-feature the same subjects. Only a confirmed
        # ERROR (or a create_post that never got accepted) leaves it unrecorded.
        if state in ("PUBLISHED", "QUEUE"):  # featured subjects -> 21-day cooldown
            # Overwrites the CLAIMED row with the final state (same tier+source_id).
            posted_log.mark_posted(
                source_type=bundle.source_type, source_id=bundle.source_id,
                tier=tkey, mode="now", text=bundle.text,
                integration_ids=[iid], postiz_post_id=pid,
                response={"channels": [{"channel": lbl, "state": state, "url": url}],
                          "keys": keys})
        elif state == "ERROR" and claimed:
            # Postiz confirmed the failure: free the week (and the subjects'
            # cooldown) so a retry can post. Any other/unknown state keeps the
            # claim on purpose — better a missed week than a double post.
            posted_log.unmark_posted(tkey, bundle.source_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
