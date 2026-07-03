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
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from _common import REPO_ROOT, integration_ids_for, load_dotenv, parse_since
from src.lib.config_loader import _TIER_DIR_BY_ID, Tier, load_tier
from src.lib.imagery import auto_media
from src.lib.posted_log import mark_posted, posted_ids_for
from src.lib.postiz_client import PostizClient
from src.lib.recipes import recipe_single
from src.lib.thread import split_for_thread

# Postgres/Temporal live in docker on the same host as this script.
PG_CONTAINER = os.getenv("POSTIZ_PG_CONTAINER", "postiz-postgres")
PG_USER = os.getenv("POSTIZ_PG_USER", "postiz-user")
PG_DB = os.getenv("POSTIZ_PG_DB", "postiz-db-local")
TEMPORAL_CONTAINER = os.getenv("POSTIZ_TEMPORAL_CONTAINER", "temporal-admin-tools")

MANUAL_QUEUE = REPO_ROOT / "data" / "manual-post-queue.md"
POLL_TIMEOUT = int(os.getenv("DAILY_POLL_TIMEOUT", "120"))  # seconds per channel
POLL_INTERVAL = 8


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

def channel_label(tier: Tier, iid: str) -> str:
    # Branch tiers inherit channels from the parent, so their own raw may not
    # carry the CHANNEL_* keys — walk up the parent chain to resolve the name.
    t: Tier | None = tier
    while t is not None:
        raw = t.raw
        if iid == raw.get("CHANNEL_X_PRIMARY"):
            return "X"
        if iid == raw.get("CHANNEL_LINKEDIN"):
            return "LinkedIn"
        for k, v in raw.items():
            if k.startswith("CHANNEL_") and v == iid:
                return k.replace("CHANNEL_", "").replace("_", " ").title()
        t = load_tier(t.parent_id) if t.parent_id else None
    return f"channel:{iid[:8]}"


def pick_newest_unposted(tier: Tier, since) -> str | None:
    """Newest unposted item from the tier's PRIMARY (first) data source.

    Each tier's DATA_SOURCE_1 is its intended daily feed (arboryx→firestore
    findings, arboryx.robotics→cards_json robotics cards). We deliberately do
    NOT mix in inherited/secondary sources: their ids belong to a different
    resolver and a cross-source id would misfire in recipe_single()."""
    from _common import build_source  # local import keeps module load cheap
    if not tier.sources:
        return None
    posted = posted_ids_for(tier.id)
    items: list[dict] = []
    ds = tier.sources[0]
    try:
        src = build_source(ds, tier)
        for it in src.list_recent(since=since, limit=25):
            sid = str(it.get("id") or it.get("catalyst_id") or it.get("_id") or "")
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
    items.sort(key=lambda it: it["_when"], reverse=True)
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


# ---------------------------------------------------------------- per-tier run

def run_tier(tier_id: str, *, push: bool, since) -> dict:
    """Process one tier. Returns a small summary dict. Never raises."""
    result = {"tier": tier_id, "status": "skipped", "channels": []}
    try:
        tier = load_tier(tier_id)
    except Exception as e:  # noqa: BLE001
        print(f"[{tier_id}] load failed: {e}", file=sys.stderr)
        result["status"] = "error"
        return result

    iids = integration_ids_for(tier)
    if not iids:
        print(f"[{tier_id}] disabled (no channels) — skip")
        return result

    source_id = pick_newest_unposted(tier, since)
    if not source_id:
        print(f"[{tier_id}] nothing new since window — skip")
        result["status"] = "nothing-new"
        return result

    try:
        bundle = recipe_single(tier, source_id)
    except Exception as e:  # noqa: BLE001
        print(f"[{tier_id}] compose failed for {source_id}: {e}", file=sys.stderr)
        result["status"] = "error"
        return result

    parts = bundle.parts if bundle.parts else split_for_thread(bundle.text)
    bundle.media_paths = auto_media(tier, bundle, "single")
    media_paths = bundle.media_paths

    print(f"[{tier_id}] {source_id} → {len(iids)} channel(s)")
    print("  " + "\n  ".join(parts[0].splitlines()))
    print(f"  [media] {media_paths[0] if media_paths else 'none'}")

    if not push:
        result["status"] = "preview"
        result["channels"] = [{"channel": channel_label(tier, i), "state": "preview"}
                              for i in iids]
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
    for iid in iids:
        label = channel_label(tier, iid)
        try:
            res = client.create_post(parts=parts, integration_ids=[iid],
                                     mode="now", media=media or None)
            post_id = res[0]["postId"] if isinstance(res, list) and res else ""
        except Exception as e:  # noqa: BLE001 — isolate one channel's failure
            print(f"    [{label}] push failed: {e}")
            queue_manual(tier, label, parts, media_paths, f"Postiz rejected the post: {e}")
            result["channels"].append({"channel": label, "state": "REJECTED"})
            continue

        state, url, err = poll_publish(post_id)
        if state == "PUBLISHED":
            any_published = True
            print(f"    [{label}] PUBLISHED → {url}")
        elif state == "ERROR":
            print(f"    [{label}] ERROR: {err or 'unknown'}")
            queue_manual(tier, label, parts, media_paths,
                         f"Publish failed: {err or 'unknown'} — see Postiz UI; "
                         f"for X this is usually depleted API credits.")
        else:  # QUEUE at timeout — workers likely down
            any_stuck = True
            print(f"    [{label}] STUCK in QUEUE after {POLL_TIMEOUT}s "
                  f"— worker pollers may be down (restart postiz).")
            queue_manual(tier, label, parts, media_paths,
                         "Stuck in QUEUE — Temporal workers likely down. "
                         "Run `docker compose restart postiz`, then retry in the Postiz UI.")
        result["channels"].append({"channel": label, "state": state, "url": url})

    # Mark the source handled UNLESS everything was merely stuck (transient infra):
    # a definitive ERROR won't improve on retry, but a dead-worker QUEUE will,
    # so leave stuck-only items unposted so the next run picks them up again.
    if any_published or not any_stuck:
        mark_posted(source_type=bundle.source_type, source_id=bundle.source_id,
                    tier=tier.id, mode="now", text=bundle.text,
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
    p.add_argument("--since", default="3d", help="how far back to look for new items")
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
    summaries = [run_tier(t, push=args.push, since=since) for t in tiers]

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
