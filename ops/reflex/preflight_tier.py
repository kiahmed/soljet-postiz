#!/usr/bin/env python3
"""Tier-config + .env + post-store half of ops/reflex/preflight.sh. Exit 1 on any FAIL.

Forked from ops/simmer/preflight_tier.py: no API token / snap URL; instead
checks the news-reactor DB connection can read public.best_signal_posts.
"""
import os
import sys

sys.path.insert(0, ".")
from bin._common import load_dotenv, integration_ids_for  # noqa: E402
from src.lib.channel_dispatch import channel_label  # noqa: E402
from src.lib.config_loader import known_tiers, load_tier  # noqa: E402

_tty = sys.stdout.isatty()
G = "\033[32m" if _tty else ""
R = "\033[31m" if _tty else ""
Y = "\033[33m" if _tty else ""
Z = "\033[0m" if _tty else ""

rc = 0


def line(name: str, status: str, detail: str = "") -> None:
    col = {"OK": G, "FAIL": R, "WARN": Y}[status]
    print(f"  {name:<42} {col}{status}{Z}   {detail}".rstrip())


def ok(n, d=""):
    line(n, "OK", d)


def bad(n, d=""):
    global rc
    line(n, "FAIL", d)
    rc = 1


def warn(n, d=""):
    line(n, "WARN", d)


tid = sys.argv[1]
load_dotenv()
P = tid.upper()

if tid not in known_tiers():
    bad("tier registered", f"'{tid}' not in {known_tiers()} — add it to _TIER_FILE_BY_ID")
    sys.exit(rc)

ok(f"tier '{tid}' registered")
t = load_tier(tid)

iids = integration_ids_for(t)
if iids:
    ok("live channels", str([channel_label(t, i) for i in iids]))
else:
    bad("live channels",
        f"no channel ids resolved — connect the accounts in Postiz, then set "
        f"POSTIZ_INTEGRATION_ID_{{X,LINKEDIN}}_{P} in .env (and LINKEDIN_ENABLED=true for LinkedIn)")

v = os.environ.get(f"POSTIZ_CUSTOMER_ID_{P}", "").strip()
ok(f"POSTIZ_CUSTOMER_ID_{P}", v) if v else warn(f"POSTIZ_CUSTOMER_ID_{P}", "unset in .env")

url = os.environ.get("REFLEX_SUPABASE_DB_URL", "").strip()
if not url:
    bad("REFLEX_SUPABASE_DB_URL", "unset — copy SUPABASE_DB_URL from facades-news-reactor/.env")
    sys.exit(rc)
host = url.split("@")[-1].split("/")[0]
ok("REFLEX_SUPABASE_DB_URL", host)
if "supabase.co" in host and "pooler" not in host:
    warn("  └ direct db host", "Cloud Run has no IPv6 egress — use the pooler URL")

try:
    import psycopg2
    table = t.raw.get("DATA_SOURCE_1_TABLE") or "public.best_signal_posts"
    conn = psycopg2.connect(url, connect_timeout=10, application_name="reflex-preflight")
    conn.set_session(readonly=True)
    with conn.cursor() as cur:
        cur.execute(f"select count(*) filter (where not posted), count(*) from {table}")
        unposted, total = cur.fetchone()
    conn.close()
    ok(f"read {table}", f"{total} post(s), {unposted} unposted")
except ImportError:
    bad("psycopg2", "pip install psycopg2-binary (make venv)")
except Exception as e:  # noqa: BLE001
    bad("read best_signal_posts", str(e).splitlines()[0][:120])

sys.exit(rc)
