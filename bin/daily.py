#!/usr/bin/env python3
"""Daily auto-poster — one post per enabled tier, published + confirmed.

Run once a day (cron / Windows Task Scheduler). For every enabled tier it:
  1. picks the newest *unposted* catalyst from that tier's data sources,
  2. composes the post text + auto-selected imagery (same pipeline as post.py),
  3. publishes to each of the tier's channels SEPARATELY, and
  4. polls Postiz until each post is PUBLISHED / ERROR — never fire-and-forget.

Resilience is the whole point:
  - Channels are dispatched one at a time, so a failing channel (e.g. X with
    depleted API credits) can't take down LinkedIn or crash the run.
  - A channel that ERRORs or gets stuck in QUEUE is appended to
    `data/manual-post-queue.md` with the exact text + image path, so a human
    can post it by hand. The same failure is also visible in the Postiz UI.
  - When X credits are topped up (or workers come back), the next run just
    succeeds — no code change, no toggle.

Preview by default; pass --push to actually publish (mirrors post.py).

Usage:
  bin/daily.py                 # preview all enabled tiers (no publish)
  bin/daily.py --push          # publish (this is the cron entry)
  bin/daily.py --tier arboryx --push
  bin/daily.py --check         # health only: worker pollers + channels, no post
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from _common import REPO_ROOT, integration_ids_for, load_dotenv, parse_since
from src.lib import content_cache
from src.lib.channel_dispatch import (
    channel_label, channel_media, channel_parts, cleanup_attach, deep_link_from_text)
from src.lib.config_loader import _TIER_DIR_BY_ID, Tier, load_tier
from src.lib.imagery import auto_media
from src.lib.posted_log import mark_posted, posted_ids_for
from src.lib.postiz_client import PostizClient
from src.lib.recipes import PostBundle, recipe_single
from src.lib.thread import split_for_thread

# Postgres/Temporal live in docker on the same host as this script.
PG_CONTAINER = os.getenv("POSTIZ_PG_CONTAINER", "postiz-postgres")
PG_USER = os.getenv("POSTIZ_PG_USER", "postiz-user")
PG_DB = os.getenv("POSTIZ_PG_DB", "postiz-db-local")
TEMPORAL_CONTAINER = os.getenv("POSTIZ_TEMPORAL_CONTAINER", "temporal-admin-tools")

MANUAL_QUEUE = REPO_ROOT / "data" / "manual-post-queue.md"
POLL_TIMEOUT = int(os.getenv("DAILY_POLL_TIMEOUT", "120"))  # seconds per channel
POLL_INTERVAL = 8
BACKLOG_LIMIT = int(os.getenv("DAILY_BACKLOG_LIMIT", "5000"))  # items the picker scans


# ---------------------------------------------------------------- infra probes

def _psql(sql: str) -> str:
    """Run a query in the Postiz postgres container; return raw stdout ('' on error)."""
    try:
        out = subprocess.check_output(
            ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB,
             "-At", "-F", "\t", "-c", sql],
            text=True, stderr=subprocess.DEVNULL, timeout=20,
        )
        return out.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def workers_alive(taskqueue: str = "linkedin") -> bool:
    """True if the Temporal worker layer is actually polling. Dead pollers are
    the #1 cause of posts silently stuck in QUEUE (orchestrator can show
    'online' while polling nothing — restart postiz to fix)."""
    try:
        out = subprocess.check_output(
            ["docker", "exec", TEMPORAL_CONTAINER, "tctl", "--address",
             "temporal:7233", "taskqueue", "describe", "--taskqueue", taskqueue,
             "--taskqueuetype", "workflow"],
            text=True, stderr=subprocess.DEVNULL, timeout=20,
        )
        return "@" in out  # poller identity rows contain an @host
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def poll_publish(post_id: str) -> tuple[str, str, str]:
    """Poll Post row until terminal. Returns (state, releaseURL, error).
    state is PUBLISHED | ERROR | QUEUE (QUEUE == still stuck at timeout)."""
    deadline = time.time() + POLL_TIMEOUT
    state, url, err = "QUEUE", "", ""
    while time.time() < deadline:
        row = _psql(
            f"SELECT state, coalesce(\"releaseURL\",''), coalesce(left(error,240),'') "
            f"FROM \"Post\" WHERE id='{post_id}';"
        )
        if row:
            parts = row.split("\t")
            state = parts[0] if parts else "QUEUE"
            url = parts[1] if len(parts) > 1 else ""
            err = parts[2] if len(parts) > 2 else ""
            if state in ("PUBLISHED", "ERROR"):
                return state, url, err
        time.sleep(POLL_INTERVAL)
    return state, url, err


# ---------------------------------------------------------------- helpers

def pick_unposted(tier: Tier, since, *, oldest: bool = False) -> str | None:
    """Newest (default) or oldest unposted item from the tier's PRIMARY source.

    Each tier's DATA_SOURCE_1 is its intended daily feed (arboryx→firestore
    findings, arboryx.robotics→firestore_cards). We deliberately do NOT mix in
    inherited/secondary sources: their ids belong to a different resolver and a
    cross-source id would misfire in recipe_single().

    oldest=True walks the backlog forward from the earliest unposted entry —
    used while per-card images are still being generated oldest-first, so the
    posts stay in step with what already has an image."""
    from _common import build_source  # local import keeps module load cheap
    if not tier.sources:
        return None
    posted = posted_ids_for(tier.id)
    items: list[dict] = []
    ds = tier.sources[0]
    try:
        src = build_source(ds, tier)
        # High cap so the WHOLE backlog is reachable — the picker takes the
        # newest unposted, so a low cap (was 25) would strand older items once
        # the newest N are all posted (e.g. 200 robotics cards).
        for it in src.list_recent(since=since, limit=BACKLOG_LIMIT):
            sid = str(it.get("id") or it.get("catalyst_id")
                      or it.get("card_id") or it.get("_id") or "")
            if sid and sid not in posted:
                it["_id"] = sid
                it["_when"] = str(it.get("timestamp") or it.get("date")
                                  or it.get("created_at") or "")
                items.append(it)
    except Exception as e:  # noqa: BLE001 — a bad source shouldn't kill the run
        print(f"    [warn] primary source {ds.type} failed: {e}", file=sys.stderr)
        return None
    if not items:
        return None
    items.sort(key=lambda it: it["_when"], reverse=not oldest)
    return items[0]["_id"]


def queue_manual(tier: Tier, label: str, parts: list[str], media: list[Path], reason: str) -> None:
    MANUAL_QUEUE.parent.mkdir(exist_ok=True)
    new_file = not MANUAL_QUEUE.exists()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = "\n\n".join(parts)
    img = str(media[0]) if media else "(none)"
    with MANUAL_QUEUE.open("a", encoding="utf-8") as f:
        if new_file:
            f.write("# Manual post queue\n\n"
                    "Posts the daily job could not publish automatically. "
                    "Post each by hand, then delete its block.\n\n---\n\n")
        f.write(f"## {stamp} — {tier.id} — {label}\n\n"
                f"**Reason:** {reason}\n\n"
                f"**Image:** `{img}`\n\n"
                f"**Text:**\n\n```\n{body}\n```\n\n---\n\n")
    print(f"    [manual] appended to {MANUAL_QUEUE.relative_to(REPO_ROOT)}")


# Per-channel imagery helpers live in src/lib/channel_dispatch (shared with
# bin/post.py): deep_link_from_text, attach_media, channel_media.


# ---------------------------------------------------------------- per-tier run

def run_tier(tier_id: str, *, push: bool, since, regenerate: bool = False,
             oldest: bool = False, channel: str | None = None) -> dict:
    """Process one tier. Returns a small summary dict. Never raises."""
    result = {"tier": tier_id, "status": "skipped", "channels": []}
    try:
        tier = load_tier(tier_id)
    except Exception as e:  # noqa: BLE001
        print(f"[{tier_id}] load failed: {e}", file=sys.stderr)
        result["status"] = "error"
        return result

    iids = integration_ids_for(tier)
    if channel:  # restrict to one channel (e.g. --channel linkedin)
        iids = [i for i in iids if channel_label(tier, i).lower() == channel.lower()]
        if not iids:
            print(f"[{tier_id}] no channel matching '{channel}' — skip")
            result["status"] = "nothing-new"
            return result
    if not iids:
        print(f"[{tier_id}] disabled (no channels) — skip")
        return result

    source_id = pick_unposted(tier, since, oldest=oldest)
    if not source_id:
        print(f"[{tier_id}] nothing new since window — skip")
        result["status"] = "nothing-new"
        return result

    # Reuse staged content if we've already generated this post (preview or a
    # prior run) — so the push publishes exactly what was previewed and never
    # silently re-composes different text. --regenerate forces a fresh compose.
    cached = None if regenerate else content_cache.load(tier.id, source_id)
    if cached:
        source_type = cached.get("source_type", "")
        text = cached["text"]
        parts = cached["parts"]
        media_paths = [Path(p) for p in cached["media_paths"]]
        staged = True
    else:
        try:
            bundle = recipe_single(tier, source_id)
        except Exception as e:  # noqa: BLE001
            print(f"[{tier_id}] compose failed for {source_id}: {e}", file=sys.stderr)
            result["status"] = "error"
            return result
        source_type = bundle.source_type
        text = bundle.text
        parts = bundle.parts if bundle.parts else split_for_thread(bundle.text)
        media_paths = auto_media(tier, bundle, "single")
        content_cache.save(tier.id, source_id, source_type=source_type, text=text,
                           parts=parts, media_paths=[str(m) for m in media_paths])
        staged = False

    print(f"[{tier_id}] {source_id} → {len(iids)} channel(s)"
          f"{'  [reusing staged content]' if staged else ''}")
    print("  " + "\n  ".join(parts[0].splitlines()))
    print(f"  [media] {media_paths[0] if media_paths else 'none'}")

    if not push:
        result["status"] = "preview"
        for i in iids:
            lbl = channel_label(tier, i)
            pol = tier.imagery_policy.get(lbl.lower(), "legacy")
            print(f"  [{lbl}] imagery: {pol}")
            result["channels"].append({"channel": lbl, "state": "preview", "imagery": pol})
        return result

    # Upload media once; reuse the ids across channels.
    client = PostizClient(api_key=os.environ.get("POSTIZ_API_KEY"))
    media: list[dict] = []
    for m in media_paths:
        if not m.exists():
            print(f"    [warn] media missing: {m}", file=sys.stderr)
            continue
        try:
            up = client.upload(m)
            if up.get("id") and up.get("path"):
                media.append({"id": up["id"], "path": up["path"]})
        except Exception as e:  # noqa: BLE001
            print(f"    [warn] upload failed ({m}): {e}", file=sys.stderr)

    any_published = any_stuck = False
    attach_cache = None  # memoized attach media, reused across 'attach' channels
    entities_cache = None  # memoized entity list for per-channel @handles
    for iid in iids:
        label = channel_label(tier, iid)
        # Per-channel imagery policy (design-per-channel-imagery.md). Absent →
        # legacy: use whatever the single-decision ladder already produced.
        ch_media, attach_cache = channel_media(
            client, tier, label, source_type=source_type, source_id=source_id,
            parts=parts, text=text, base_media=media, attach_cache=attach_cache)
        # Per-channel @handles (Figure → @figure on LinkedIn, @Figure_robots on X).
        ch_parts, entities_cache = channel_parts(
            tier, label, source_type=source_type, source_id=source_id,
            parts=parts, entities_cache=entities_cache)
        policy = tier.imagery_policy.get(label.lower(), "legacy")
        if policy == "attach" and not ch_media:
            print(f"    [{label}] no attach image — degrading to link_card")
        print(f"    [{label}] imagery: {policy} → {len(ch_media)} media")
        pol_note = f" [imagery: {policy}]"
        try:
            res = client.create_post(parts=ch_parts, integration_ids=[iid],
                                     mode="now", media=ch_media or None)
            post_id = res[0]["postId"] if isinstance(res, list) and res else ""
        except Exception as e:  # noqa: BLE001 — isolate one channel's failure
            print(f"    [{label}] push failed: {e}")
            queue_manual(tier, label, ch_parts, media_paths, f"Postiz rejected the post: {e}{pol_note}")
            result["channels"].append({"channel": label, "state": "REJECTED"})
            continue

        state, url, err = poll_publish(post_id)
        if state == "PUBLISHED":
            any_published = True
            print(f"    [{label}] PUBLISHED → {url}")
        elif state == "ERROR":
            print(f"    [{label}] ERROR: {err or 'unknown'}")
            queue_manual(tier, label, ch_parts, media_paths,
                         f"Publish failed: {err or 'unknown'} — see Postiz UI; "
                         f"for X this is usually depleted API credits.{pol_note}")
        else:  # QUEUE at timeout — workers likely down
            any_stuck = True
            print(f"    [{label}] STUCK in QUEUE after {POLL_TIMEOUT}s "
                  f"— worker pollers may be down (restart postiz).")
            queue_manual(tier, label, ch_parts, media_paths,
                         "Stuck in QUEUE — Temporal workers likely down. "
                         f"Run `docker compose restart postiz`, then retry in the Postiz UI.{pol_note}")
        result["channels"].append({"channel": label, "state": state, "url": url})

    if any_published:  # posted → drop the one-shot image + the staged content JSON
        cleanup_attach(attach_cache)
        content_cache.delete(tier.id, source_id)

    # Mark the source handled UNLESS everything was merely stuck (transient infra):
    # a definitive ERROR won't improve on retry, but a dead-worker QUEUE will,
    # so leave stuck-only items unposted so the next run picks them up again.
    if any_published or not any_stuck:
        mark_posted(source_type=source_type, source_id=source_id,
                    tier=tier.id, mode="now", text=text,
                    integration_ids=iids,
                    postiz_post_id=",".join(c.get("url", "") for c in result["channels"]),
                    response={"channels": result["channels"]})
        result["status"] = "posted"
    else:
        print(f"    [{tier_id}] left UNPOSTED for retry (all channels stuck).")
        result["status"] = "stuck"
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tier", help="run just one tier id (default: all enabled)")
    p.add_argument("--push", action="store_true", help="actually publish (default: preview)")
    p.add_argument("--since", default="3650d",
                   help="how far back to look for items (default ~all; the "
                        "posted-log prevents repeats, so backlogs drip one/day)")
    p.add_argument("--regenerate", action="store_true",
                   help="re-compose text/imagery even if staged content exists")
    p.add_argument("--oldest", action="store_true",
                   help="pick the OLDEST unposted item instead of the newest "
                        "(walk the backlog forward, e.g. while card images catch up)")
    p.add_argument("--channel", help="publish to ONE channel only (e.g. linkedin, x)")
    p.add_argument("--check", action="store_true",
                   help="health check only (worker pollers + channels), no posting")
    args = p.parse_args()

    load_dotenv()

    if args.check:
        alive = workers_alive()
        print(f"Temporal workers polling: {'YES' if alive else 'NO — run: docker compose restart postiz'}")
        for tid in ([args.tier] if args.tier else _TIER_DIR_BY_ID):
            try:
                t = load_tier(tid)
                iids = integration_ids_for(t)
                labels = [channel_label(t, i) for i in iids] or ["(disabled)"]
                print(f"  {tid:<20} → {', '.join(labels)}")
            except Exception as e:  # noqa: BLE001
                print(f"  {tid:<20} → error: {e}")
        return 0 if alive else 1

    since = parse_since(args.since)
    if args.push and not workers_alive():
        print("WARNING: Temporal workers are not polling — posts will queue and not "
              "publish. Run `docker compose restart postiz` first.", file=sys.stderr)

    tiers = [args.tier] if args.tier else list(_TIER_DIR_BY_ID)
    summaries = [run_tier(t, push=args.push, since=since, regenerate=args.regenerate,
                          oldest=args.oldest, channel=args.channel)
                 for t in tiers]

    print("\n==== summary ====")
    for s in summaries:
        chans = ", ".join(
            f"{c['channel']}:{c['state']}" for c in s["channels"]
        ) if s["channels"] else "-"
        print(f"  {s['tier']:<20} {s['status']:<12} {chans}")
    # Non-zero exit if anything needs a human (stuck) so cron logs flag it.
    return 1 if any(s["status"] == "stuck" for s in summaries) else 0


if __name__ == "__main__":
    sys.exit(main())
