#!/usr/bin/env python3
"""Health of every social channel the tiers post to, from Postiz's own store.

Postiz is the source of truth for channel auth: it flips `disabled` /
`refreshNeeded` on an integration the moment the platform rejects its token
(401/403), and records post-time failures (X credit depletion, etc.) in the
`Errors` table. So this reads those instead of re-implementing OAuth signing
for every provider — no external calls, no secrets leave the box.

For each active channel it reports:
  - product (Postiz Customer) + provider + connection name
  - STATUS: OK | REFRESH-NEEDED | DISABLED | TOKEN-EXPIRED | DELETED | NO-INTEGRATION
    (REFRESH-NEEDED / DISABLED == Postiz already saw an auth failure — a live
     probe would 401/403)
  - token expiry, last successful post (from posted_log), and the latest
    platform error (surfaces X "credits depleted", which X masks as a generic
    error — see the note printed for X)

Usage: bin/social-status.py [--tier arboryx.robotics] [--all-tiers]
The Postiz DB is reached via `docker exec postiz-postgres psql` (the port is
not published to the host), matching how heal.sh talks to the stack.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from bin._common import load_dotenv, integration_ids_for  # noqa: E402
from src.lib.config_loader import load_tier  # noqa: E402
from src.lib import posted_log  # noqa: E402

import os  # noqa: E402

TIERS = ["arboryx", "arboryx.robotics"]
PG_CONTAINER = os.environ.get("POSTIZ_PG_CONTAINER", "postiz-postgres")


def psql(sql: str) -> list[list[str]]:
    """Run a query in the Postiz Postgres container; return pipe-split rows."""
    user = os.environ.get("POSTGRES_USER", "postiz-user")
    db = os.environ.get("POSTGRES_DB", "postiz-db-local")
    p = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", user, "-d", db, "-tA", "-F", "|", "-c", sql],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"psql failed: {p.stderr.strip() or p.stdout.strip()}")
    return [ln.split("|") for ln in p.stdout.splitlines() if ln]


def integrations() -> dict[str, dict]:
    rows = psql(
        'select i.id, i."providerIdentifier", i.name, i.disabled, i."refreshNeeded", '
        '(i."deletedAt" is not null), coalesce(to_char(i."tokenExpiration",\'YYYY-MM-DD\'),\'\'), '
        'coalesce(c.name,\'\') '
        'from "Integration" i left join "Customer" c on c.id = i."customerId"'
    )
    out = {}
    for r in rows:
        out[r[0]] = dict(provider=r[1], name=r[2], disabled=r[3] == "t",
                         refresh_needed=r[4] == "t", deleted=r[5] == "t",
                         token_exp=r[6], product=r[7])
    return out


def latest_errors() -> dict[str, dict]:
    """Latest error per platform (x / linkedin / …)."""
    rows = psql(
        'select distinct on (platform) platform, to_char("createdAt",\'YYYY-MM-DD HH24:MI\'), '
        'left(coalesce(message,\'\'),100), left(coalesce(body,\'\'),240) '
        'from "Errors" order by platform, "createdAt" desc'
    )
    return {r[0]: dict(when=r[1], message=r[2], body=r[3]) for r in rows}


def status_of(intg: dict | None) -> str:
    if intg is None:
        return "NO-INTEGRATION"
    if intg["deleted"]:
        return "DELETED"
    if intg["disabled"]:
        return "DISABLED"
    if intg["refresh_needed"]:
        return "REFRESH-NEEDED"
    exp = intg["token_exp"]
    if exp and exp < datetime.now(timezone.utc).strftime("%Y-%m-%d"):
        return "TOKEN-EXPIRED"
    return "OK"


def last_post_for(intg_id: str) -> str:
    """Most recent posted_log entry that touched this integration id."""
    try:
        import sqlite3
        db = REPO_ROOT / "data" / "posted_log.sqlite"
        if not db.exists():
            return ""
        c = sqlite3.connect(db)
        row = c.execute(
            "select posted_at, tier from posted where integration_ids like ? "
            "order by posted_at desc limit 1", (f"%{intg_id}%",),
        ).fetchone()
        return f"{row[0][:16]} ({row[1]})" if row else "—"
    except Exception:
        return "?"


def platform_of(provider: str) -> str:
    return "x" if provider.startswith("x") else ("linkedin" if "linkedin" in provider else provider)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", help="only this tier")
    ap.add_argument("--all-tiers", action="store_true", help="all tiers (default)")
    args = ap.parse_args()
    load_dotenv()

    tiers = [args.tier] if args.tier else TIERS

    # integration id -> (tiers using it, enabled?)
    usage: dict[str, dict] = {}
    for tid in tiers:
        t = load_tier(tid)
        enabled = set(integration_ids_for(t))
        for cid in t.channels:
            u = usage.setdefault(cid, {"tiers": set(), "enabled": False})
            u["tiers"].add(tid)
            if cid in enabled:
                u["enabled"] = True

    try:
        intgs = integrations()
        errs = latest_errors()
    except RuntimeError as e:
        print(f"ERROR reading Postiz DB: {e}\nIs the '{PG_CONTAINER}' container up?", file=sys.stderr)
        return 1

    print(f"Social channel status  ({datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC})\n")
    any_bad = False
    for cid, u in sorted(usage.items(), key=lambda kv: (intgs.get(kv[0], {}).get("product", ""), kv[0])):
        intg = intgs.get(cid)
        st = status_of(intg)
        if st not in ("OK",):
            any_bad = True
        gate = "enabled" if u["enabled"] else "gated-off"
        prod = intg["product"] if intg else "?"
        prov = intg["provider"] if intg else "?"
        name = intg["name"] if intg else "(no Integration row for this id)"
        print(f"● {prod:<12} {prov:<13} {name}")
        print(f"    id={cid}  [{gate} in {', '.join(sorted(u['tiers']))}]")
        exp = f"  token-exp={intg['token_exp']}" if intg and intg["token_exp"] else ""
        print(f"    STATUS: {st}{exp}")
        print(f"    last post: {last_post_for(cid)}")
        if intg:
            e = errs.get(platform_of(prov))
            if e:
                credit = ""
                blob = (e["message"] + e["body"]).lower()
                if "credit" in blob or "402" in blob or "unknown error" in blob:
                    credit = "  <- possible X credit depletion (X masks it as a generic error)" if prov.startswith("x") else ""
                print(f"    last {platform_of(prov)} error: {e['when']}  {e['message']}{credit}")
        print()

    print("Notes:")
    print("  REFRESH-NEEDED / DISABLED = Postiz already saw the platform reject the token")
    print("  (a live re-probe would return 401/403). Reconnect the channel in the Postiz UI.")
    print("  X exposes no read-only credit balance; depletion only shows as a post-time")
    print("  error above. Top up at developer.x.com if X errors are credit-related.")
    return 2 if any_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
