#!/usr/bin/env python3
"""Resolve social handles for the entities in a post — store-verified, plus
optional LLM discovery for the ones the store doesn't know yet.

Usage:
  bin/resolve-handles.py --text "Schaeffler Group deploys Humanoid ..."
  bin/resolve-handles.py --card-id ROB-051326-003 --tier arboryx.robotics
  bin/resolve-handles.py --text "..." --discover        # LLM-suggest the gaps
  bin/resolve-handles.py --text "..." --channel linkedin # one channel only
  bin/resolve-handles.py --card-id ROB-... --json

Store handles are VERIFIED and safe to post. Discovered ones are marked
UNVERIFIED — confirm them before adding to products/_shared/handles.json (a
wrong @ tags the wrong account).
"""
from __future__ import annotations

import argparse
import json
import sys

from _common import load_dotenv
from src.lib.handles import handle_for
from src.lib.llm import chat as llm_chat

CHANNELS = ("linkedin", "x")


def _entities_from_card(card_id: str, tier_id: str) -> list[str]:
    from _common import build_source
    from src.lib.composer import primary_entities
    from src.lib.config_loader import load_tier
    tier = load_tier(tier_id)
    card = build_source(tier.sources[0], tier).get(card_id)
    return [e.get("name") for e in primary_entities(card, n=6) if e.get("name")]


def _entities_from_text(text: str) -> list[str]:
    """LLM-extract the organizations/companies named in the post text."""
    out = llm_chat(
        "You extract named organizations from social post text.",
        "List ONLY the distinct company/organization names in the post below, "
        "one per line, no bullets, no commentary. If none, output nothing.\n\n"
        f"POST:\n{text}",
        max_tokens=200,
    ) or ""
    seen, names = set(), []
    for line in out.splitlines():
        n = line.strip().lstrip("-*• ").strip()
        if n and n.lower() not in seen and len(n) < 60:
            seen.add(n.lower())
            names.append(n)
    return names


def _discover(entity: str, channel: str) -> str | None:
    """Best-effort LLM lookup for a handle. UNVERIFIED — the model may be wrong."""
    plat = "LinkedIn" if channel == "linkedin" else "X (Twitter)"
    out = (llm_chat(
        "You return official social media handles. Never guess.",
        f"What is the official {plat} handle for the company '{entity}'? "
        f"Reply with ONLY the handle starting with '@' (e.g. @figure-ai), or the "
        f"single word 'unknown' if you are not confident. Do not guess.",
        max_tokens=40,
    ) or "").strip().split()
    if not out:
        return None
    tok = out[0].strip().strip(".,")
    return tok if tok.startswith("@") else None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="post text to resolve handles for")
    src.add_argument("--card-id", help="resolve from a KG card's primary entities")
    p.add_argument("--tier", default="arboryx.robotics", help="tier for --card-id")
    p.add_argument("--channel", choices=CHANNELS, help="one channel only (default: both)")
    p.add_argument("--discover", action="store_true",
                   help="LLM-suggest handles the store doesn't have (UNVERIFIED)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    load_dotenv()
    channels = [args.channel] if args.channel else list(CHANNELS)

    if args.card_id:
        entities = _entities_from_card(args.card_id, args.tier)
    else:
        entities = _entities_from_text(args.text)

    rows = []
    for e in entities:
        rec = {"entity": e}
        for ch in channels:
            h = handle_for(e, ch)
            if h:
                rec[ch] = {"handle": h, "source": "store", "verified": True}
            elif args.discover:
                d = _discover(e, ch)
                rec[ch] = {"handle": d, "source": "llm", "verified": False} if d \
                    else {"handle": None, "source": None, "verified": False}
            else:
                rec[ch] = {"handle": None, "source": None, "verified": False}
        rows.append(rec)

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    if not rows:
        print("(no entities found)")
        return 0
    w = max(len(r["entity"]) for r in rows)
    for r in rows:
        cells = []
        for ch in channels:
            c = r[ch]
            if c["handle"]:
                tag = "" if c["verified"] else "  ⚠UNVERIFIED"
                cells.append(f"{ch}={c['handle']}{tag}")
            else:
                cells.append(f"{ch}=—")
        print(f"  {r['entity']:<{w}}  " + "   ".join(cells))
    if args.discover:
        print("\n⚠ UNVERIFIED handles are LLM guesses — confirm before adding to "
              "products/_shared/handles.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
