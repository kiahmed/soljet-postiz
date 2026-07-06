#!/usr/bin/env python3
"""Unified post CLI — recipe-driven.

Recipes:
  single          — one Firestore finding or one KG catalyst → one post
  narrative       — file-based post from products/<tier>/narratives/<slug>.md
  sector-digest   — top N sector findings → one summarized post
  event           — ad-hoc title/body/link/media for one-off posts

Common flags:
  --tier <id>           parent ('arboryx') or branch ('arboryx.robotics')
  --push                actually create in Postiz (default: print only)
  --mode draft|schedule|now
  --at <iso> | --in 2h|30m|1d   (only for --mode schedule)
  --force               repost even if already in posted_log

Recipe-specific flags listed via --help.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _common import REPO_ROOT, integration_ids_for, load_dotenv, parse_since
from src.lib.config_loader import load_tier
from src.lib.posted_log import is_posted, mark_posted
from src.lib.channel_dispatch import channel_label, channel_media, channel_parts
from src.lib.postiz_client import PostizClient
from src.lib.recipes import (
    PostBundle,
    recipe_event,
    recipe_narrative,
    recipe_sector_digest,
    recipe_single,
)
from src.lib.thread import split_for_thread
from src.lib.imagery import auto_media


def _copy_to_clipboard(text: str) -> str | None:
    """Try common clipboard utilities; return the tool name on first success."""
    candidates = [
        ("clip.exe", ["clip.exe"]),                              # WSL → Windows
        ("pbcopy", ["pbcopy"]),                                  # macOS
        ("wl-copy", ["wl-copy"]),                                # Wayland
        ("xclip", ["xclip", "-selection", "clipboard"]),         # X11
        ("xsel", ["xsel", "--clipboard", "--input"]),            # X11 alt
    ]
    for name, cmd in candidates:
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p.communicate(text.encode("utf-8"), timeout=5)
            if p.returncode == 0:
                return name
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
    return None


def _show_image(path: Path) -> str | None:
    """Open `path` in the system viewer; return the tool name on first success."""
    if not path.exists():
        return None
    candidates: list[tuple[str, list[str]]] = []
    # WSL: cmd.exe start opens with the default Windows app
    try:
        win_path = subprocess.check_output(
            ["wslpath", "-w", str(path)], text=True,
            stderr=subprocess.DEVNULL, timeout=3,
        ).strip()
        candidates.append(("explorer", ["cmd.exe", "/c", "start", "", win_path]))
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    candidates.extend([
        ("open", ["open", str(path)]),         # macOS
        ("xdg-open", ["xdg-open", str(path)]), # Linux
    ])
    for name, cmd in candidates:
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return name
        except (FileNotFoundError, OSError):
            continue
    return None


def _resolve_publish_date(args) -> str | None:
    if args.at:
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
    p.add_argument("--recipe", required=True,
                   choices=["single", "narrative", "sector-digest", "event"])
    p.add_argument("--tier", required=True)
    p.add_argument("--push", action="store_true")
    p.add_argument("--mode", default="draft", choices=["draft", "schedule", "now"])
    p.add_argument("--at")
    p.add_argument("--in", dest="delay")
    p.add_argument("--force", action="store_true")
    # recipe-specific
    p.add_argument("--source-id")
    p.add_argument("--slug")
    p.add_argument("--sector")
    p.add_argument("--since", default="7d")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--title")
    p.add_argument("--body", default="")
    p.add_argument("--link", default="")
    p.add_argument("--media")
    p.add_argument("--no-llm", action="store_true",
                   help="(event recipe) skip LLM rewrite, use title/body/link verbatim")
    p.add_argument("--no-auto-media", action="store_true",
                   help="skip automatic imagery selection (use only explicit media)")
    p.add_argument("--copy", action="store_true",
                   help="copy the post text (or full thread) to the system clipboard")
    p.add_argument("--show", action="store_true",
                   help="open the generated image in the system viewer")
    p.add_argument("--channel", help="publish to ONE channel only (e.g. linkedin, x)")
    args = p.parse_args()

    load_dotenv()
    tier = load_tier(args.tier)

    if args.recipe == "single":
        if not args.source_id:
            raise SystemExit("--source-id required for --recipe single")
        bundle = recipe_single(tier, args.source_id)
    elif args.recipe == "narrative":
        if not args.slug:
            raise SystemExit("--slug required for --recipe narrative")
        bundle = recipe_narrative(tier, args.slug, REPO_ROOT)
    elif args.recipe == "sector-digest":
        if not args.sector:
            raise SystemExit("--sector required for --recipe sector-digest")
        bundle = recipe_sector_digest(tier, args.sector, parse_since(args.since), args.limit)
    elif args.recipe == "event":
        if not args.title:
            raise SystemExit("--title required for --recipe event")
        bundle = recipe_event(
            tier,
            title=args.title,
            body=args.body,
            link=args.link,
            media=Path(args.media) if args.media else None,
            llm_rewrite=not args.no_llm,
        )
    else:
        raise SystemExit(f"unknown recipe: {args.recipe}")

    parts = bundle.parts if bundle.parts else split_for_thread(bundle.text)

    # Auto-pick imagery unless caller opts out or media was supplied explicitly.
    if not args.no_auto_media:
        bundle.media_paths = auto_media(tier, bundle, args.recipe)

    print("---- DRAFT ----")
    if len(parts) == 1:
        print(parts[0])
    else:
        for i, p in enumerate(parts, 1):
            print(f"--- {i}/{len(parts)} ({len(p)} chars) ---")
            print(p)
    for m in bundle.media_paths:
        print(f"[media] {m}")
    if not bundle.media_paths and not args.no_auto_media:
        print("[media] none (auto-imagery: no good candidate)")
    print("---- /DRAFT ----")

    if args.copy:
        # Multi-part threads copy with blank-line separators so manual posters
        # can paste once and split between tweets visually.
        clip_text = "\n\n".join(parts) if len(parts) > 1 else parts[0]
        tool = _copy_to_clipboard(clip_text)
        print(f"[copied] {len(clip_text)} chars → clipboard via {tool}" if tool
              else "[copy] no clipboard tool found (install xclip / wl-copy / pbcopy / clip.exe)",
              file=sys.stderr)

    if args.show:
        if bundle.media_paths:
            tool = _show_image(bundle.media_paths[0])
            print(f"[opened] {bundle.media_paths[0]} via {tool}" if tool
                  else f"[show] no opener found for {bundle.media_paths[0]}",
                  file=sys.stderr)
        else:
            print("[show] no image to open", file=sys.stderr)

    if not args.push:
        return 0

    if not args.force and is_posted(bundle.source_type, bundle.source_id, tier.id):
        print(f"already posted: {bundle.source_type}/{bundle.source_id} on {tier.id} (use --force)")
        return 0

    iids = integration_ids_for(tier)
    if args.channel:  # restrict to one channel (e.g. --channel linkedin)
        iids = [i for i in iids if channel_label(tier, i).lower() == args.channel.lower()]
        if not iids:
            print(f"no channel matching '{args.channel}'", file=sys.stderr)
            return 1
    if not iids:
        print("no integration IDs to push to (channels empty or all gated off)", file=sys.stderr)
        return 1

    client = PostizClient(api_key=os.environ.get("POSTIZ_API_KEY"))
    media: list[dict] = []
    for m in bundle.media_paths:
        if not m.exists():
            print(f"media not found: {m}", file=sys.stderr)
            return 1
        up = client.upload(m)
        if up.get("id") and up.get("path"):
            media.append({"id": up["id"], "path": up["path"]})

    publish_date = _resolve_publish_date(args)

    if tier.imagery_policy:
        # Per-channel imagery: split into one create_post per integration so the
        # image[] list can differ (X link_card / LinkedIn attach). Content is
        # identical across channels; only media differs.
        res_all = []
        attach_cache = None
        entities_cache = None
        for iid in iids:
            label = channel_label(tier, iid)
            ch_media, attach_cache = channel_media(
                client, tier, label, source_type=bundle.source_type,
                source_id=bundle.source_id, parts=parts, text=bundle.text,
                base_media=media, attach_cache=attach_cache)
            ch_parts, entities_cache = channel_parts(
                tier, label, source_type=bundle.source_type,
                source_id=bundle.source_id, parts=parts, entities_cache=entities_cache)
            print(f"  [{label}] imagery: {tier.imagery_policy.get(label.lower(),'legacy')} "
                  f"→ {len(ch_media)} media")
            r = client.create_post(parts=ch_parts, integration_ids=[iid], mode=args.mode,
                                   publish_date=publish_date, media=ch_media or None)
            if isinstance(r, list):
                res_all.extend(r)
        res = res_all
    else:
        res = client.create_post(
            parts=parts,
            integration_ids=iids,
            mode=args.mode,
            publish_date=publish_date,
            media=media or None,
        )

    postiz_post_id = ""
    if isinstance(res, list) and res:
        # Postiz returns [{postId, integration}, ...] — one per integration.
        postiz_post_id = ",".join(str(p.get("postId") or p.get("id") or "") for p in res if p.get("postId") or p.get("id"))
    elif isinstance(res, dict):
        postiz_post_id = str(res.get("id") or res.get("postId") or "")

    mark_posted(
        source_type=bundle.source_type,
        source_id=bundle.source_id,
        tier=tier.id,
        mode=args.mode,
        text=bundle.text,
        integration_ids=iids,
        postiz_post_id=postiz_post_id,
        response=res if isinstance(res, dict) else {"raw": res},
    )

    when = "as draft" if args.mode == "draft" else f"mode={args.mode} date={publish_date or 'now'}"
    print(f"pushed → {len(iids)} channel(s), {len(media)} media, {when}, postiz_post_id={postiz_post_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
