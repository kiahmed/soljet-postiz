#!/usr/bin/env python3
"""Inspect / clear / refresh the local social handle→URN caches.

  make social-cache-list   <channel> [entity...]   show matching entries
  make social-cache-clean  <channel> <entity...>   drop them → re-resolve next post
  make social-cache-update <channel> <entity...>   drop + re-resolve the SAME slug
                                                    now (refresh a stale record;
                                                    does NOT invent a new slug)

  channel  linkedin  (the only channel with a URN cache today; x handles aren't
                     resolved to URNs, so they aren't cached)
  entity   free-form, CASE-INSENSITIVE. Matches every cache entry whose slug or
           org name shares the entity's tokens (len ≥ 3) OR contains its
           compacted form as a substring (so 'spirit ai' matches
           'spiritai-robotics'). Omit for -list to dump everything.

Examples:
  make social-cache-list   linkedin
  make social-cache-clean  linkedin galaxea ai      # clear the stale galaxea rows
  make social-cache-update linkedin spirit ai

(The op is in the target name, not a positional word, so a bare `update` goal
can't collide with the `make update` stack target. This script takes the op as
its first argv, supplied by those make targets.)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CACHES = {"linkedin": REPO / "data" / "linkedin_urn_cache.json"}


def _toks(s: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(w) >= 3]


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _matches(entity: str, slug: str, name: str) -> bool:
    """A cache entry matches an entity by EITHER: all entity tokens (len≥3)
    present in the slug/name, OR the entity's compacted form as a substring of
    the compacted slug/name. The substring path catches run-together slugs like
    'spiritai-robotics' for the entity 'Spirit AI'."""
    etok = _toks(entity)
    ecomp = _compact(entity)
    for text in (slug, name):
        toks = set(_toks(text.replace("-", " ")))
        if etok and all(t in toks for t in etok):
            return True
        if len(ecomp) >= 4 and ecomp in _compact(text):
            return True
    return False


def _dump(sel: dict) -> None:
    for slug, v in sorted(sel.items()):
        v = v or {}
        oid = v.get("id")
        print(f"  {slug:30} {'urn:li:organization:'+str(oid) if oid else '(miss)':32} "
              f"{v.get('name', '')}")


def main(argv: list[str]) -> int:
    if not argv or argv[0].lower() in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if argv else 2
    op = argv[0].lower()
    channel = (argv[1].lower() if len(argv) > 1 else "linkedin")
    entity = " ".join(argv[2:])

    if op not in ("list", "delete", "update"):
        print(f"unknown op '{op}' — use list | delete | update")
        return 2
    path = CACHES.get(channel)
    if not path:
        print(f"no URN cache for channel '{channel}' (have: {', '.join(CACHES)})")
        return 2
    if not path.exists():
        print(f"cache is empty ({path.name} does not exist yet)")
        return 0

    cache = json.loads(path.read_text())
    if op != "list" and not entity.strip():
        print(f"refusing '{op}' with no entity — name one (e.g. 'galaxea ai')")
        return 2

    sel = {k: v for k, v in cache.items()
           if not entity.strip() or _matches(entity, k, (v or {}).get("name", ""))}
    if not sel:
        print(f"no {channel} cache entries match '{entity or '(all)'}'")
        return 0

    _dump(sel)

    if op == "list":
        return 0

    if op == "delete":
        for k in list(sel):
            cache.pop(k, None)
        path.write_text(json.dumps(cache, indent=2, sort_keys=True))
        print(f"→ deleted {len(sel)} entry(ies) from {channel}; they re-resolve on "
              f"the next post/preview.")
        return 0

    # update — drop then re-resolve each slug against LinkedIn
    sys.path.insert(0, str(REPO))
    from src.lib import linkedin_urn as L  # noqa: E402
    print("→ refreshing:")
    for slug in list(sel):
        cache.pop(slug, None)
        path.write_text(json.dumps(cache, indent=2, sort_keys=True))
        res = L.resolve(slug)  # repopulates the cache file
        cache = json.loads(path.read_text())
        print(f"    {slug:30} "
              + (f"urn:li:organization:{res['id']} {res.get('name', '')}"
                 if res else "MISS (still unresolvable)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
