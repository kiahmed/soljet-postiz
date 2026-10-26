#!/usr/bin/env python3
"""Weekly traffic report — GA4 (both platforms) + Postiz post counts → one HTML.

    make ga-weekly [WEEKS=n]

Writes docs/weekly_ga_updates.html. The page carries its own data (a JSON
block inside the HTML), so every run re-reads it, upserts the current week
(week-to-date) and the previous week (GA finalises data 24-48h late), and
re-renders. Running it several times in a week just refreshes that week.
Newest week first. WEEKS=n backfills the n most recent weeks (default: 2,
or 6 on the very first run when the page has no data yet).

Platforms / products (GA hostName → product):
  Arboryx  (GA property GA_PROPERTY_ARBORYX, default 538475687)
           arboryx.ai, robotics.arboryx.ai
  Facades  (GA property GA_PROPERTY_FACADES, default 557273440)
           simmer./matrix./reflex.facades.trade, facades.trade

Auth: an access token from the active gcloud account (needs Viewer on both
GA properties and analyticsdata.googleapis.com enabled on the quota
project GA_QUOTA_PROJECT, default $GCP_PROJECT). Post counts come from the
local Postiz postgres container; if it isn't running the page says so.

Recommendations are rule-based (thresholds below) — deterministic and free;
they flag what to look at, not a verdict.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "bin"))

from _common import load_dotenv  # noqa: E402

OUT = REPO_ROOT / "docs" / "weekly_ga_updates.html"
TZ = ZoneInfo("America/New_York")
DATA_RE = re.compile(r'<script type="application/json" id="ga-weekly-data">(.*?)</script>', re.S)
UTM_LIVE = date(2026, 10, 21)  # post links UTM-tagged from here (commit 8c905f3)
TEST_SOURCES = {"tagassistant.google.com"}  # GA tag-debugging visits, not people

# Postiz Integration.name → product key; GA hostName → product key.
PLATFORMS = {
    "Arboryx": {
        "env": "GA_PROPERTY_ARBORYX", "default": "538475687",
        "tracking_since": "2026-10-22",  # GA tag deployed on arboryx.ai / robotics
        "hosts": {"arboryx.ai": "arboryx", "www.arboryx.ai": "arboryx",
                  "robotics.arboryx.ai": "robotics"},
        "postiz": {"Arboryx.ai": "arboryx"},
        "names": {"arboryx": "Arboryx.ai (main + Robotics posts)", "robotics": "Robotics branch"},
    },
    "Facades": {
        "env": "GA_PROPERTY_FACADES", "default": "557273440",
        "tracking_since": "2026-10-03",  # first GA data on *.facades.trade
        "hosts": {"simmer.facades.trade": "simmer", "matrix.facades.trade": "matrix",
                  "reflex.facades.trade": "reflex", "facades.trade": "home",
                  "www.facades.trade": "home"},
        "postiz": {"Simmer": "simmer", "Matrix": "matrix", "Reflex": "reflex",
                   "Facades.trade": "home"},
        "names": {"simmer": "Simmer", "matrix": "Matrix", "reflex": "Reflex",
                  "home": "facades.trade (home)"},
    },
}
SIGNUP_EVENTS = ("sign_up",)  # sent by each site on a successful sign-up
START_EVENT = "sign_up_start"  # first keystroke in sign-up mode


# ---------------------------------------------------------------- GA access

def _token() -> str:
    out = subprocess.run(
        ["gcloud", "auth", "print-access-token",
         "--scopes=https://www.googleapis.com/auth/analytics.readonly"],
        capture_output=True, text=True, timeout=60)
    tok = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    if out.returncode or not tok:
        sys.exit(f"gcloud token failed: {out.stderr.strip()[:300]}")
    return tok


class GA:
    def __init__(self, quota_project: str):
        self.h = {"Authorization": f"Bearer {_token()}",
                  "x-goog-user-project": quota_project}

    def report(self, prop: str, start: date, end: date, dims: list[str],
               mets: list[str], *, events: tuple[str, ...] = (), limit: int = 250) -> list[dict]:
        """runReport → [{dim: val, ..., met: float}]. Test-tool traffic dropped."""
        flt = {"notExpression": {"filter": {"fieldName": "sessionSource", "inListFilter":
               {"values": sorted(TEST_SOURCES)}}}}
        if events:
            flt = {"andGroup": {"expressions": [flt, {"filter": {
                "fieldName": "eventName", "inListFilter": {"values": list(events)}}}]}}
        body = {"dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
                "dimensions": [{"name": d} for d in dims],
                "metrics": [{"name": m} for m in mets],
                "dimensionFilter": flt, "limit": limit}
        r = requests.post(f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
                          headers=self.h, json=body, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"GA {prop}: {r.status_code} {r.text[:300]}")
        rows = []
        for row in r.json().get("rows", []):
            d = {k: v["value"] for k, v in zip(dims, row["dimensionValues"])}
            d.update({k: float(v["value"]) for k, v in zip(mets, row["metricValues"])})
            rows.append(d)
        return rows


# ---------------------------------------------------------------- collection

def _product(plat: dict, host: str) -> str:
    return plat["hosts"].get(host.lower(), "other")


def _posts(start: date, end: date) -> dict | None:
    """{Integration.name: {channel: n}} for published top-level posts, or None."""
    pg = [os.getenv("POSTIZ_PG_CONTAINER", "postiz-postgres"),
          os.getenv("POSTIZ_PG_USER", "postiz-user"), os.getenv("POSTIZ_PG_DB", "postiz-db-local")]
    sql = ("select i.name, i.\"providerIdentifier\", count(*) from \"Post\" p "
           "join \"Integration\" i on i.id = p.\"integrationId\" "
           "where p.state = 'PUBLISHED' and p.\"deletedAt\" is null and p.\"parentPostId\" is null "
           f"and (p.\"publishDate\" at time zone 'America/New_York')::date between '{start}' and '{end}' "
           "group by 1, 2")
    try:
        out = subprocess.check_output(
            ["docker", "exec", pg[0], "psql", "-U", pg[1], "-d", pg[2], "-At", "-F", "\t", "-c", sql],
            text=True, stderr=subprocess.DEVNULL, timeout=20)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    res: dict = {}
    for line in out.strip().splitlines():
        name, prov, n = line.split("\t")
        chan = prov.split("-")[0]  # linkedin-page → linkedin
        res.setdefault(name, {})[chan] = res.get(name, {}).get(chan, 0) + int(n)
    return res


def collect_week(ga: GA, monday: date, today: date) -> dict:
    end = min(monday + timedelta(days=6), today)
    week = {"week": monday.isoformat(), "start": monday.isoformat(), "end": end.isoformat(),
            "complete": monday + timedelta(days=6) < today,
            "updated": datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z"), "platforms": {}}
    posts = _posts(monday, end)
    week["posts_available"] = posts is not None
    for pname, plat in PLATFORMS.items():
        prop = os.getenv(plat["env"], plat["default"])
        prods: dict = {}

        def P(key):
            return prods.setdefault(key, {
                "users": 0, "new_users": 0, "sessions": 0, "engaged": 0, "eng_secs": 0,
                "views": 0, "form_starts": 0, "signup_starts": 0, "signups": 0, "posts": {},
                "sources": [], "devices": {}, "pages": [], "hosts": set()})

        for r in ga.report(prop, monday, end, ["hostName"],
                           ["activeUsers", "newUsers", "sessions", "engagedSessions",
                            "userEngagementDuration", "screenPageViews"]):
            p = P(_product(plat, r["hostName"]))
            p["hosts"].add(r["hostName"])
            for k, m in (("users", "activeUsers"), ("new_users", "newUsers"), ("sessions", "sessions"),
                         ("engaged", "engagedSessions"), ("eng_secs", "userEngagementDuration"),
                         ("views", "screenPageViews")):
                p[k] += r[m]
        for r in ga.report(prop, monday, end,
                           ["hostName", "sessionSource", "sessionMedium", "sessionCampaignName"],
                           ["sessions", "engagedSessions"]):
            P(_product(plat, r["hostName"]))["sources"].append(
                {"source": r["sessionSource"], "medium": r["sessionMedium"],
                 "campaign": r["sessionCampaignName"], "sessions": r["sessions"],
                 "engaged": r["engagedSessions"]})
        for r in ga.report(prop, monday, end, ["hostName", "deviceCategory"],
                           ["sessions", "engagedSessions"]):
            d = P(_product(plat, r["hostName"]))["devices"].setdefault(
                r["deviceCategory"], {"sessions": 0, "engaged": 0})
            d["sessions"] += r["sessions"]
            d["engaged"] += r["engagedSessions"]
        for r in ga.report(prop, monday, end, ["hostName", "pagePath"], ["screenPageViews"], limit=100):
            P(_product(plat, r["hostName"]))["pages"].append(
                {"path": r["pagePath"], "views": r["screenPageViews"]})
        for r in ga.report(prop, monday, end, ["hostName", "eventName"], ["eventCount"],
                           events=("form_start", START_EVENT) + SIGNUP_EVENTS):
            p = P(_product(plat, r["hostName"]))
            field = {"form_start": "form_starts", START_EVENT: "signup_starts"}.get(r["eventName"], "signups")
            p[field] += r["eventCount"]
        for iname, chans in (posts or {}).items():
            key = plat["postiz"].get(iname)
            if key:
                for ch, n in chans.items():
                    P(key)["posts"][ch] = P(key)["posts"].get(ch, 0) + n
        for p in prods.values():
            p["hosts"] = sorted(p["hosts"])
            p["sources"].sort(key=lambda s: -s["sessions"])
            p["sources"] = p["sources"][:8]
            p["pages"].sort(key=lambda s: -s["views"])
            p["pages"] = p["pages"][:5]
        week["platforms"][pname] = {"property": prop, "products": prods}
    week["recs"] = []  # filled by recommend() once the previous week is known
    return week


# ---------------------------------------------------------------- recommendations

def _n_posts(p: dict) -> int:
    return int(sum(p.get("posts", {}).values()))


def _unattributed(p: dict) -> float:
    s = sum(x["sessions"] for x in p["sources"])
    d = sum(x["sessions"] for x in p["sources"] if x["source"] in ("(direct)", "(not set)"))
    return d / s if s else 0.0


def recommend(week: dict, prev: dict | None) -> list[dict]:
    """[{level: attention|opportunity|info, text}] — plain-English next steps."""
    recs: list[dict] = []
    add = lambda lvl, txt: recs.append({"level": lvl, "text": txt})  # noqa: E731
    for pname, plat in week["platforms"].items():
        names = PLATFORMS[pname]["names"]
        since = PLATFORMS[pname]["tracking_since"]
        if week["end"] < since:
            continue  # no GA tag yet: visits can't be judged
        tracked_all_week = week["start"] >= since
        prev_prods = (prev or {}).get("platforms", {}).get(pname, {}).get("products", {})
        best = None
        for key, p in plat["products"].items():
            nm = names.get(key, f"Other ({', '.join(p['hosts']) or key})")
            posts, users, sess = _n_posts(p), int(p["users"]), p["sessions"]
            if posts >= 3 and not sess and tracked_all_week:
                add("attention", f"<b>{nm}</b>: {posts} posts went out but the site got no visits. "
                    "Check the post links work and the GA tag is on that site.")
            elif posts >= 5 and sess / posts < 0.2 and tracked_all_week:
                add("opportunity", f"<b>{nm}</b>: {posts} posts brought only {int(sess)} visits. "
                    "Post fewer but stronger items (one clear finding + a reason to click), "
                    "and try a second channel (X or Reddit).")
            if sess >= 5 and _unattributed(p) > 0.6 and week["start"] >= UTM_LIVE.isoformat():
                add("attention", f"<b>{nm}</b>: {_unattributed(p):.0%} of visits show as direct. "
                    "Check the posters were redeployed with UTM tagging.")
            mob = p["devices"].get("mobile", {})
            if mob.get("sessions", 0) >= 3 and mob["engaged"] / mob["sessions"] < 0.25:
                add("attention", f"<b>{nm}</b>: mobile visitors leave almost immediately "
                    f"({int(mob['engaged'])}/{int(mob['sessions'])} engaged). Most social clicks are "
                    "on phones. Open the site on a phone and fix layout or load time.")
            st, su = int(p.get("signup_starts", 0)), int(p["signups"])
            if st and st > su:
                add("attention" if st - su >= 3 else "info",
                    f"<b>{nm}</b>: {st - su} of {st} sign-ups started were abandoned. "
                    "Try the form yourself on a phone; shorten it or explain what they get.")
            elif p["form_starts"] and not su and not st:
                fs = int(p["form_starts"])
                add("attention", f"<b>{nm}</b>: {fs} {'person' if fs == 1 else 'people'} started the sign-up "
                    "form but no submit was recorded. Either the form loses them, or the site "
                    "doesn't send a <code>sign_up</code> event on success. Add it so sign-ups are counted.")
            pu = prev_prods.get(key, {}).get("users", 0)
            if pu >= 5 and week["complete"]:
                ch = (users - pu) / pu
                if ch <= -0.3:
                    add("attention", f"<b>{nm}</b>: visitors down {abs(ch):.0%} vs last week "
                        f"({int(pu)} → {users}). Compare what was posted.")
                elif ch >= 0.3:
                    add("opportunity", f"<b>{nm}</b>: visitors up {ch:.0%} vs last week "
                        f"({int(pu)} → {users}). Find the post/source behind it and repeat it.")
            if posts == 0 and _n_posts(prev_prods.get(key, {})):
                add("info", f"<b>{nm}</b>: nothing was posted this week.")
            if sess >= 5:
                rate = p["engaged"] / sess
                if best is None or rate > best[1]:
                    best = (nm, rate, sess)
            src = [s for s in p["sources"] if s["medium"] not in ("(none)", "(not set)")
                   and s["source"] not in ("(direct)",) and s["sessions"] >= 2]
            if src:
                top = max(src, key=lambda s: s["engaged"])
                add("opportunity", f"<b>{nm}</b>: best real source is <b>{html.escape(top['source'])}"
                    f" / {html.escape(top['medium'])}</b> ({int(top['sessions'])} visits, "
                    f"{int(top['engaged'])} engaged). Put more effort there.")
        if best:
            add("info", f"{pname}: <b>{best[0]}</b> holds visitors best this week "
                f"({best[1]:.0%} of {int(best[2])} visits engaged). Use it as the lead product in posts.")
    order = {"attention": 0, "opportunity": 1, "info": 2}
    return sorted(recs, key=lambda r: order[r["level"]])


# ---------------------------------------------------------------- HTML

def _fmt_secs(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def _pct(a: float, b: float) -> str:
    return f"{a / b:.0%}" if b else "–"


def _delta(cur: float, prev: float | None) -> str:
    if prev is None or prev == 0:
        return ""
    ch = (cur - prev) / prev
    cls = "up" if ch > 0 else "down" if ch < 0 else ""
    return f' <span class="d {cls}">{"+" if ch > 0 else ""}{ch:.0%}</span>'


def render_week(w: dict, prev: dict | None, open_: bool) -> str:
    s, e = date.fromisoformat(w["start"]), date.fromisoformat(w["end"])
    status = "complete" if w["complete"] else "in progress (week to date)"
    tot_u = sum(p["users"] for pl in w["platforms"].values() for p in pl["products"].values())
    tot_p = sum(_n_posts(p) for pl in w["platforms"].values() for p in pl["products"].values())
    out = [f'<details class="week" {"open" if open_ else ""}><summary>'
           f'<span class="wk">Week of {s:%b %d} – {e:%b %d, %Y}</span>'
           f'<span class="meta">{status} · {int(tot_u)} visitors · {tot_p} posts · '
           f'updated {html.escape(w["updated"])}</span></summary>']
    if w["recs"]:
        out.append('<h3>Next steps / attention</h3><ul class="recs">')
        for r in w["recs"]:
            out.append(f'<li class="{r["level"]}"><span class="tag">{r["level"]}</span>{r["text"]}</li>')
        out.append("</ul>")
    if not w.get("posts_available", True):
        out.append('<p class="note">Post counts unavailable: the Postiz database wasn\'t '
                   'reachable when this ran.</p>')
    for pname, pl in w["platforms"].items():
        names = PLATFORMS[pname]["names"]
        pprev = (prev or {}).get("platforms", {}).get(pname, {}).get("products", {})
        out.append(f'<h3>{pname} <span class="muted">GA property {pl["property"]}</span></h3>')
        since = PLATFORMS[pname]["tracking_since"]
        if w["end"] < since:
            out.append(f'<p class="note">GA tracking wasn\'t on these sites yet (live from {since}): '
                       'visit numbers are missing, only post counts are real.</p>')
        elif w["start"] < since:
            out.append(f'<p class="note">GA tracking went live mid-week ({since}): visits '
                       'cover only part of the week.</p>')
        if not pl["products"]:
            out.append('<p class="note">No visits or posts recorded.</p>')
            continue
        out.append('<div class="scroll"><table><thead><tr><th>Product</th><th>Visitors</th>'
                   '<th>New</th><th>Visits</th><th>Engaged</th><th>Avg time / visit</th>'
                   '<th>Posts</th><th>Visits / post</th><th>Form starts</th><th>Sign-up started</th>'
                   '<th>Sign-ups</th><th>Abandoned</th>'
                   '</tr></thead><tbody>')
        for key, p in sorted(pl["products"].items(), key=lambda kv: -kv[1]["users"]):
            nm = names.get(key, f"Other ({', '.join(p['hosts']) or key})")
            np_ = _n_posts(p)
            vpp = f"{p['sessions'] / np_:.2f}" if np_ else "–"
            chans = ", ".join(f"{c} {n}" for c, n in sorted(p["posts"].items()))
            out.append(
                f'<tr><td>{html.escape(nm)}</td>'
                f'<td>{int(p["users"])}{_delta(p["users"], pprev.get(key, {}).get("users"))}</td>'
                f'<td>{int(p["new_users"])}</td><td>{int(p["sessions"])}</td>'
                f'<td>{_pct(p["engaged"], p["sessions"])}</td>'
                f'<td>{_fmt_secs(p["eng_secs"] / p["sessions"]) if p["sessions"] else "–"}</td>'
                f'<td title="{html.escape(chans)}">{np_}</td>'
                f'<td>{vpp}</td>'
                f'<td>{int(p["form_starts"])}</td><td>{int(p.get("signup_starts", 0))}</td>'
                f'<td>{int(p["signups"])}</td>'
                f'<td>{max(int(p.get("signup_starts", 0)) - int(p["signups"]), 0) or "–"}</td></tr>')
        out.append("</tbody></table></div>")
        for key, p in sorted(pl["products"].items(), key=lambda kv: -kv[1]["users"]):
            if not p["sources"] and not p["pages"]:
                continue
            nm = names.get(key, f"Other ({', '.join(p['hosts']) or key})")
            src = "".join(
                f'<tr><td>{html.escape(x["source"])} / {html.escape(x["medium"])}</td>'
                f'<td>{html.escape(x["campaign"]) if x["campaign"] not in ("(direct)", "(not set)", "(referral)", "(organic)") else ""}</td>'
                f'<td>{int(x["sessions"])}</td><td>{_pct(x["engaged"], x["sessions"])}</td></tr>'
                for x in p["sources"])
            dev = " · ".join(f'{d} {int(v["sessions"])} ({_pct(v["engaged"], v["sessions"])} engaged)'
                             for d, v in sorted(p["devices"].items(), key=lambda kv: -kv[1]["sessions"]))
            pages = " · ".join(f'<code>{html.escape(x["path"][:60])}</code> {int(x["views"])}'
                               for x in p["pages"])
            out.append(f'<details class="prod"><summary>{html.escape(nm)}: where visitors came from'
                       f'</summary><div class="scroll"><table><thead><tr><th>Source / medium</th>'
                       f'<th>Campaign</th><th>Visits</th><th>Engaged</th></tr></thead><tbody>{src}'
                       f'</tbody></table></div><p><b>Devices:</b> {dev or "–"}</p>'
                       f'<p><b>Top pages:</b> {pages or "–"}</p></details>')
    out.append("</details>")
    return "\n".join(out)


GUIDE = """
<header>
  <h1>Weekly traffic report</h1>
  <p class="lede">Who visited the Arboryx and Facades sites each week, where they came from,
  and whether our social posts are bringing the right people. One section per week, newest
  first, each ending in a short list of things to do next.</p>
</header>
<section class="guide">
  <div class="card"><h2>What it shows</h2><ul>
    <li><b>Two platforms, each product separately:</b> Arboryx (arboryx.ai, robotics.arboryx.ai)
      and Facades (Simmer, Matrix, Reflex, facades.trade). Data comes from Google Analytics 4.</li>
    <li><b>Posts vs. visits:</b> how many posts each product published that week (from Postiz)
      next to the visits it got. <i>Visits / post</i> shows whether posting more actually pays.</li>
    <li><b>Where visitors came from:</b> LinkedIn, X, Reddit, search, other sites. Post links
      carry UTM tags since Oct 21 2026, so each visit is credited to its channel and product
      (<i>campaign</i> = product).</li>
    <li><b>Quality, not just count:</b> <i>Engaged</i> = visits that stayed 10s+, viewed 2+
      pages or converted. <i>Avg time / visit</i> shows real reading time.</li>
    <li><b>Sign-up funnel:</b> <i>Sign-up started</i> = typed into the sign-up form
      (<code>sign_up_start</code>), <i>Sign-ups</i> = account created (<code>sign_up</code>),
      <i>Abandoned</i> = started but not finished. <i>Form starts</i> is GA's automatic count for
      any form (search, contact, login), kept for context. Live on Facades sites from Oct 26 2026.</li>
  </ul></div>
  <div class="card"><h2>What you get from it</h2><ul>
    <li>Which product and channel bring <b>engaged</b> visitors, so you know where to put effort.</li>
    <li>Early warnings: posts that bring no clicks, mobile visitors bouncing, sign-up forms
      abandoned, traffic drops.</li>
    <li>A short <b>Next steps</b> list per week. <span class="tag attention">attention</span>
      something is broken or leaking, <span class="tag opportunity">opportunity</span> do more
      of what works, <span class="tag info">info</span> context.</li>
  </ul></div>
  <div class="card"><h2>How to use it</h2><ol>
    <li>Once a week (Monday works well), from the soljet-postiz folder run
      <code>make ga-weekly</code>, then open this file in a browser.</li>
    <li>Read the newest week's <b>Next steps</b> first (2 minutes), then the product table.</li>
    <li>Running it again in the same week only refreshes that week; older weeks are kept.
      <code>make ga-weekly WEEKS=8</code> rebuilds the last 8 weeks.</li>
    <li>Needs: gcloud signed in as an account with <i>Viewer</i> on both GA properties, and the
      Postiz stack running for post counts.</li>
  </ol></div>
  <div class="card"><h2>Reading the numbers</h2><ul>
    <li><b>Visitors</b> = people (devices); <b>Visits</b> = sessions. One person can visit
      several times.</li>
    <li><b>direct / (none)</b> = GA couldn't tell where they came from (typed URL, app
      in-browser clicks, untagged links).</li>
    <li>The current week is <i>in progress</i>, and GA fills in the last 1–2 days late, so
      judge trends on complete weeks.</li>
    <li>Small numbers swing a lot. 3 → 6 visitors is "+100%" but not a trend.</li>
  </ul></div>
</section>
"""

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1f21;--muted:#6b6f76;--card:#fff;--line:#e4e1da;--accent:#2f6f5e;
--att:#b4441f;--opp:#2f6f5e;--info:#4a5a78;--up:#2f7d32;--down:#b4441f}
@media (prefers-color-scheme:dark){:root{--bg:#141517;--fg:#e8e6e1;--muted:#9a9ea6;--card:#1c1e21;
--line:#2c2f33;--accent:#7cc4ad;--att:#f08a62;--opp:#7cc4ad;--info:#9fb1d6;--up:#7bd389;--down:#f08a62}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1080px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:28px;margin:0 0 6px}h2{font-size:17px;margin:0 0 8px}h3{font-size:16px;margin:22px 0 8px}
.lede{color:var(--muted);max-width:760px;margin:0 0 20px}
.guide{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin-bottom:34px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.card ul,.card ol{margin:0;padding-left:18px}.card li{margin:4px 0}
.weeks-h{font-size:20px;border-top:1px solid var(--line);padding-top:24px}
details.week{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:4px 18px 14px;margin:0 0 14px}
details.week>summary{cursor:pointer;padding:12px 0;display:flex;flex-wrap:wrap;gap:4px 14px;align-items:baseline}
.wk{font-weight:650;font-size:16px}.meta,.muted{color:var(--muted);font-size:13px;font-weight:400}
.recs{list-style:none;padding:0;margin:0}.recs li{padding:7px 0;border-bottom:1px solid var(--line)}
.tag{display:inline-block;font-size:11px;text-transform:uppercase;letter-spacing:.04em;border-radius:4px;
padding:1px 6px;margin-right:8px;border:1px solid currentColor}
.attention .tag,.tag.attention{color:var(--att)}.opportunity .tag,.tag.opportunity{color:var(--opp)}
.info .tag,.tag.info{color:var(--info)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 10px 6px 0;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500;font-size:12.5px}
.d{font-size:12px}.d.up{color:var(--up)}.d.down{color:var(--down)}
details.prod{margin:8px 0}details.prod>summary{cursor:pointer;color:var(--accent)}
code{font-size:12.5px;background:color-mix(in srgb,var(--line) 55%,transparent);padding:1px 4px;border-radius:4px}
.note{color:var(--muted);font-style:italic}
"""


def render(weeks: list[dict]) -> str:
    weeks = sorted(weeks, key=lambda w: w["week"], reverse=True)
    body = [render_week(w, weeks[i + 1] if i + 1 < len(weeks) else None, i == 0)
            for i, w in enumerate(weeks)]
    data = json.dumps(weeks, separators=(",", ":")).replace("</", "<\\/")
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Weekly Traffic Report</title><style>{CSS}</style></head><body><main>'
            f'{GUIDE}<h2 class="weeks-h">Weekly data</h2>{"".join(body) or "<p class=note>No data yet.</p>"}'
            f'</main><script type="application/json" id="ga-weekly-data">{data}</script>'
            f'</body></html>\n')


def load_weeks() -> list[dict]:
    if not OUT.exists():
        return []
    m = DATA_RE.search(OUT.read_text(encoding="utf-8"))
    return json.loads(m.group(1).replace("<\\/", "</")) if m else []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--weeks", type=int, default=0,
                    help="weeks to (re)fetch, newest back (default 2; 6 on first run)")
    args = ap.parse_args()
    load_dotenv()
    weeks = {w["week"]: w for w in load_weeks()}
    n = args.weeks or (2 if weeks else 6)
    today = datetime.now(TZ).date()
    this_monday = today - timedelta(days=today.weekday())
    ga = GA(os.getenv("GA_QUOTA_PROJECT") or os.getenv("GCP_PROJECT") or "marketresearch-agents")
    for i in reversed(range(n)):
        mon = this_monday - timedelta(weeks=i)
        print(f"  fetching week of {mon} ...")
        w = collect_week(ga, mon, today)
        has_any = any(pl["products"] for pl in w["platforms"].values())
        if has_any or mon.isoformat() in weeks or i == 0:
            weeks[w["week"]] = w
    ordered = sorted(weeks.values(), key=lambda w: w["week"])
    for i, w in enumerate(ordered):  # recs need the previous week for trends
        w["recs"] = recommend(w, ordered[i - 1] if i else None)
    OUT.write_text(render(list(weeks.values())), encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO_ROOT)} ({len(weeks)} weeks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
