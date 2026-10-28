#!/usr/bin/env python3
"""Post ONE weekly-recap card as its own post (no digest, no mixing).

  bin/recap_card_post.py --tier arboryx.robotics --card entity-sap --channel linkedin            # preview
  bin/recap_card_post.py ... --push --mode now                                                  # publish
  bin/recap_card_post.py ... --push --mode schedule --at 2026-10-28T19:30:00+00:00               # Postiz schedules it
  bin/recap_card_post.py ... --push --mode draft                                                # draft (X by hand)

Text = CKG's verdict + the card's insight_headline (verbatim) + the card's own
hashtags + the company's @handle/LinkedIn mention (same path as card posts); one
chart image; link = Graph Explorer for that company, last 30 days. Records the
company in the 21-day digest cooldown. Spec: docs/graph-posters.md "Weekly recap".
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily  # noqa: E402  (poll_publish, _kind_channel_enabled)
from bin._common import integration_ids_for, load_dotenv  # noqa: E402
from graph_stats_post import _recap_image  # noqa: E402
from src.lib import channel_dispatch, posted_log  # noqa: E402
from src.lib.channel_dispatch import channel_label, channel_parts  # noqa: E402
from src.lib.composer import compose_recap_card, kg_explorer_url  # noqa: E402
from src.lib.config_loader import load_tier  # noqa: E402
from src.lib.funnel import append_link_to_text  # noqa: E402
from src.lib.postiz_client import PostizClient  # noqa: E402
from src.lib.recipes import insights_tkey  # noqa: E402
from src.lib.sources.factory import build_source  # noqa: E402
from src.lib.thread import length_fn_for_channel, max_chars_for_channel, split_for_thread  # noqa: E402

_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", required=True)
    ap.add_argument("--card", required=True, help="recap card id, e.g. entity-sap")
    ap.add_argument("--channel", required=True, help="linkedin | x")
    ap.add_argument("--week", help="ISO week (default: this week), e.g. 2026-W44")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--mode", choices=["draft", "schedule", "now"], default="draft")
    ap.add_argument("--at", help="ISO datetime for --mode schedule")
    args = ap.parse_args()
    if not _ID.fullmatch(args.card):
        raise SystemExit(f"bad card id: {args.card!r}")

    load_dotenv()
    tier = load_tier(args.tier)
    week = args.week or datetime.now(timezone.utc).strftime("%G-W%V")
    iid = next((i for i in integration_ids_for(tier)
                if channel_label(tier, i).lower() == args.channel.lower()), None)
    if not iid:
        raise SystemExit(f"no live channel '{args.channel}' for {tier.id}")
    lbl = channel_label(tier, iid)

    src = next((build_source(ds, tier) for ds in tier.sources if ds.type == "firestore_cards"), None)
    recap = src.recap_week(week) if src is not None else None
    card = next((c for c in (recap or {}).get("cards", []) if c.get("id") == args.card), None)
    if not card or card.get("kind") != "entity" or not card.get("entity"):
        raise SystemExit(f"recap {week}: no entity card '{args.card}' (have: "
                         f"{[c.get('id') for c in (recap or {}).get('cards', [])]})")
    entity = str(card["entity"])
    sector = str((recap or {}).get("sector") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", sector):
        raise SystemExit("recap record has no usable sector")

    source_id = f"RECAP-{tier.id}-{week}-{card['id']}"
    if posted_log.is_posted("recap_card", source_id, insights_tkey(tier.id, lbl)):
        raise SystemExit(f"already posted: {source_id} on {lbl}")

    base = f"https://{tier.raw.get('PUBLISH_SUBDOMAIN', '').strip()}" if tier.raw.get("PUBLISH_SUBDOMAIN") else ""
    link = kg_explorer_url(base, entity, days=30)
    text = append_link_to_text(compose_recap_card(tier, card), link)
    channel_dispatch._ctx_cache[source_id] = text
    parts = split_for_thread(text, max_chars=max_chars_for_channel(lbl), length_fn=length_fn_for_channel(lbl))
    parts, _ = channel_parts(tier, lbl, source_type="recap_card", source_id=source_id,
                             parts=parts, entities_cache=[{"name": entity}])
    img = _recap_image(tier, f"recaps/{sector}/{week}/{card['id']}.png", {})
    print(f"--- [{lbl}] {source_id} ---\n[media] {img}")
    for p in parts:
        print(p + "\n")
    if not args.push:
        print("(preview only — pass --push)")
        return 0
    if not img:
        raise SystemExit("recap image missing — not posting a chart-less recap card")
    if args.mode == "schedule" and not args.at:
        raise SystemExit("--mode schedule needs --at")

    client = PostizClient(api_key=os.environ.get("POSTIZ_API_KEY"))
    up = client.upload(img)
    resp = client.create_post(parts=parts, integration_ids=[iid], mode=args.mode,
                              media=[{"id": up["id"], "path": up["path"]}],
                              publish_date=args.at if args.mode == "schedule" else None)
    r = resp[0] if isinstance(resp, list) and resp else resp
    pid = (r or {}).get("postId") or (r or {}).get("id")
    state, url = args.mode.upper(), ""
    if args.mode == "now":
        state, url, err = daily.poll_publish(pid)
        url = url or err
    print(f"[{lbl}] {state} {url or pid}")
    if args.mode != "draft" and state in ("PUBLISHED", "QUEUE", "SCHEDULE"):
        posted_log.mark_posted(
            source_type="recap_card", source_id=source_id, tier=insights_tkey(tier.id, lbl),
            mode=args.mode, text=text, integration_ids=[iid], postiz_post_id=pid,
            response={"channels": [{"channel": lbl, "state": state, "url": url}],
                      "keys": [f"e:{entity}"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
