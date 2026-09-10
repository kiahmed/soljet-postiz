#!/usr/bin/env python3
"""Tier-config + .env half of ops/simmer/preflight.sh. Exit 1 on any FAIL."""
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
        f"no channel ids resolved — set POSTIZ_INTEGRATION_ID_*_{P} in .env "
        f"(and LINKEDIN_ENABLED=true for LinkedIn)")

for key in (f"POSTIZ_CUSTOMER_ID_{P}", "SIMMER_API_BASE", "SIMMER_API_TOKEN"):
    v = os.environ.get(key, "").strip()
    ok(key, v if key != "SIMMER_API_TOKEN" else v[:6] + "…") if v else bad(key, "unset in .env")

snap = os.environ.get("SIMMER_SNAP_URL", "").strip()
if snap:
    ok("SIMMER_SNAP_URL", snap)
else:
    warn("SIMMER_SNAP_URL", "empty — posts go text-only until the snap service is deployed")

sys.exit(rc)
