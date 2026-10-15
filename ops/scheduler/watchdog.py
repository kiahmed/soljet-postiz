#!/usr/bin/env python3
"""Postiz failure watchdog — runs inside the scheduler-trigger container.

Every product and channel (local Arboryx poster, cloud matrix/simmer posters)
ends up as a row in Postiz's `Post` table, so this is the one place that sees
all of them. Alert-only: it NEVER reposts. A post that Postiz marked ERROR is
emailed once, to the owner of that product:

    Arboryx*            -> ALERT_TO_ARBORYX
    everything else     -> ALERT_TO_FACADES   (Matrix, Simmer, Reflex, Torque, ...)

X "credits depleted" (HTTP 402) is skipped unless ALERT_ON_X_CREDITS_DEPLETED=true
— X is posted by hand while the balance is empty, so alerting would be noise.

Also kicks `catch-up.sh watchdog` each cycle (GCP mode only; local mode has its
own cron) so posts missed while the host was asleep/Postiz was down get filled.

Config (env wins over /app/.env): SMTP_HOST (default smtp.gmail.com), SMTP_PORT,
SMTP_USER, SMTP_PASSWORD, SMTP_STARTTLS, SMTP_USE_SSL, ALERT_FROM, ALERT_TO_*,
ALERT_ON_X_CREDITS_DEPLETED, ALERT_WINDOW_HOURS (24), WATCHDOG_INTERVAL_MIN (15),
WATCHDOG_DRY=1 (print, don't send or mark).
"""
import base64
import json
import os
import re
import smtplib
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
STATE = os.path.join(REPO, "data", "watchdog_alerted.json")

PG_CONTAINER = os.environ.get("POSTIZ_PG_CONTAINER", "postiz-postgres")
PG_USER = os.environ.get("POSTIZ_PG_USER", "postiz-user")
PG_DB = os.environ.get("POSTIZ_PG_DB", "postiz-db-local")


def _log(msg):
    sys.stderr.write(f"[watchdog] {msg}\n")
    sys.stderr.flush()


def _dotenv():
    out = {}
    try:
        with open(os.path.join(REPO, ".env"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k.strip()] = v
    except OSError:
        pass
    return out


def _cfg(env, key, default=""):
    return os.environ.get(key) or env.get(key) or default


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _query_errors(window_h):
    sql = f"""
      select coalesce(json_agg(t), '[]'::json) from (
        select p.id, i.name as product, i."providerIdentifier" as provider,
               p."publishDate" as at, left(p.content, 140) as content, p.error
        from "Post" p join "Integration" i on i.id = p."integrationId"
        where p.state = 'ERROR' and p."deletedAt" is null and p."parentPostId" is null
          and p."createdAt" > now() - interval '{int(window_h)} hours'
        order by p."publishDate"
      ) t;"""
    r = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB, "-t", "-A", "-c", sql],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout.strip() or "[]")


def _decoded_error(err):
    """Postiz stores Temporal's failure JSON; the provider's real response is a
    base64 payload inside it. Return (lowercased searchable text, short reason)."""
    err = err or ""
    blobs = [err]
    for m in re.finditer(r'"data":"([A-Za-z0-9+/=]{16,})"', err):
        try:
            blobs.append(base64.b64decode(m.group(1)).decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            pass
    text = " ".join(blobs)
    # the payload is itself JSON-in-JSON; unescape so substrings match
    flat = text.replace('\\"', '"').lower()
    msg = ""
    m = re.search(r'"message":"([^"]{1,160})"', err)
    if m:
        msg = m.group(1)
    return flat, msg


def _classify(row):
    flat, msg = _decoded_error(row.get("error"))
    credits = "credits depleted" in flat or "creditsdepleted" in flat or "credits-depleted" in flat
    if credits:
        reason = "X API credits depleted (HTTP 402)"
    elif "aggregateerror" in flat or "econn" in flat or "enotfound" in flat or "etimedout" in flat:
        reason = "network error reaching the platform"
    else:
        reason = msg or "unknown error"
    return credits, reason


def _recipient(product, env):
    if (product or "").lower().startswith("arboryx"):
        return _cfg(env, "ALERT_TO_ARBORYX", "arborix.platform@gmail.com")
    return _cfg(env, "ALERT_TO_FACADES", "edgelane.products.support@gmail.com")


def _load_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(st):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    st = {k: v for k, v in st.items() if v >= cutoff}
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    os.replace(tmp, STATE)


def _send(env, to, subject, body):
    user = _cfg(env, "SMTP_USER")
    pw = _cfg(env, "SMTP_PASSWORD")
    if not user or not pw:
        raise RuntimeError("SMTP_USER/SMTP_PASSWORD not set")
    host = _cfg(env, "SMTP_HOST", "smtp.gmail.com")
    port = int(_cfg(env, "SMTP_PORT", "587"))
    msg = EmailMessage()
    msg["From"] = _cfg(env, "ALERT_FROM", "soljet-postiz_svc-alert@solutionjet.net")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if _truthy(_cfg(env, "SMTP_USE_SSL", "false")):
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            if _truthy(_cfg(env, "SMTP_STARTTLS", "true")):
                s.starttls()
            s.login(user, pw)
            s.send_message(msg)


def run_once():
    """One alert pass. Returns number of posts alerted on."""
    env = _dotenv()
    dry = _truthy(os.environ.get("WATCHDOG_DRY", ""))
    alert_credits = _truthy(_cfg(env, "ALERT_ON_X_CREDITS_DEPLETED", "false"))
    rows = _query_errors(_cfg(env, "ALERT_WINDOW_HOURS", "24"))
    state = _load_state()

    groups = {}  # recipient -> [(row, reason)]
    for row in rows:
        if row["id"] in state:
            continue
        credits, reason = _classify(row)
        if credits and not alert_credits:
            continue
        groups.setdefault(_recipient(row["product"], env), []).append((row, reason))

    sent = 0
    for to, items in groups.items():
        lines = [f"{len(items)} post(s) failed to publish in Postiz. Not retried automatically.", ""]
        for row, reason in items:
            lines.append(f"- {row['product']} / {row['provider']} @ {str(row['at'])[:16]} UTC")
            lines.append(f"    reason : {reason}")
            lines.append(f"    post   : {row['content']!r}")
            lines.append(f"    id     : {row['id']}")
        subject = f"[postiz] {len(items)} failed post(s): " + ", ".join(
            sorted({i[0]['product'] for i in items}))
        body = "\n".join(lines)
        if dry:
            _log(f"DRY would mail {to}: {subject}\n{body}")
            continue
        try:
            _send(env, to, subject, body)
        except Exception as e:  # noqa: BLE001
            _log(f"mail to {to} failed ({e}) — will retry next cycle")
            continue
        now = datetime.now(timezone.utc).isoformat()
        for row, _ in items:
            state[row["id"]] = now
        _save_state(state)
        sent += len(items)
        _log(f"alerted {to}: {len(items)} post(s)")
    return sent


def _catch_up():
    env = _dotenv()
    if _cfg(env, "GCP_PROD_SCHEDULER") != "enabled":
        return  # local mode: the scheduler container's own cron runs the watchdog
    try:
        log = open(os.path.join(REPO, "data", "daily.log"), "a")
        subprocess.run(["bash", os.path.join(HERE, "catch-up.sh"), "watchdog"],
                       stdout=log, stderr=log, timeout=3600)
    except Exception as e:  # noqa: BLE001
        _log(f"catch-up failed: {e}")


def loop():
    env = _dotenv()
    every = max(1, int(_cfg(env, "WATCHDOG_INTERVAL_MIN", "15"))) * 60
    _log(f"started; every {every // 60} min")
    while True:
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            _log(f"alert pass failed: {e}")
        _catch_up()
        time.sleep(every)


if __name__ == "__main__":
    if "--once" in sys.argv:
        print(run_once())
    else:
        loop()
