#!/usr/bin/env python3
"""Tiny HTTP trigger that lets an external timer (GCP Cloud Scheduler) fire the
daily posting run on the host where the stack + data + docker socket live.

Cloud Scheduler can only make an HTTP call — it has no runtime for daily.py's
docker-exec confirmation, local posted_log dedup, or KG card PNGs. So in GCP
mode this sidecar is the "backend": Scheduler POSTs here (through the Cloudflare
tunnel), we validate + kick off ops/scheduler/run-daily.sh for one channel, and
return 202 immediately. A real run can take hours (60m between cards), so we
NEVER block the response on it — the job runs detached and logs to data/daily.log.

Endpoints:
  GET  /healthz            -> 200 "ok"
  POST /run                -> 202; body {"channel","count"?,"delay"?,"tier"?}
                              header  Authorization: Bearer $SCHEDULER_TRIGGER_TOKEN

Env:
  SCHEDULER_TRIGGER_TOKEN  required shared secret (reject all /run without it)
  SCHEDULER_TRIGGER_PORT   listen port (default 8090)
  SCHEDULER_DRY_RUN=1      pass through to run-daily.sh (no heal, no posting)
"""
import hmac
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
RUN_DAILY = os.path.join(HERE, "run-daily.sh")
LOG = os.path.join(REPO, "data", "daily.log")


def _load_channels():
    """{channel: (count, delay, tier)} from channels.conf — the whitelist."""
    conf = os.path.join(HERE, "channels.conf")
    out = {}
    with open(conf) as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = [c.strip() for c in line.split("|")]
            if len(parts) < 4 or not parts[0]:
                continue
            ch, count, delay, tier = parts[0], parts[1], parts[2], parts[3]
            out[ch] = (count, delay, tier)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "postiz-scheduler-trigger"

    def _reply(self, code, msg):
        body = (msg if msg.endswith("\n") else msg + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quieter, single-line
        sys.stderr.write("[trigger] %s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        if self.path.rstrip("/") == "/healthz":
            return self._reply(200, "ok")
        return self._reply(404, "not found")

    def _authed(self):
        want = os.environ.get("SCHEDULER_TRIGGER_TOKEN", "")
        if not want:
            self._reply(503, "trigger not configured: SCHEDULER_TRIGGER_TOKEN unset")
            return False
        got = self.headers.get("Authorization", "")
        prefix = "Bearer "
        got = got[len(prefix):] if got.startswith(prefix) else ""
        # constant-time compare; hmac.compare_digest tolerates length mismatch
        if not (got and hmac.compare_digest(got, want)):
            self._reply(401, "unauthorized")
            return False
        return True

    def do_POST(self):
        if self.path.rstrip("/") != "/run":
            return self._reply(404, "not found")
        if not self._authed():
            return
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except (ValueError, json.JSONDecodeError):
            return self._reply(400, "bad json")

        channels = _load_channels()
        channel = str(payload.get("channel", "")).strip()
        if channel not in channels:
            # whitelist: never exec an arbitrary channel string
            return self._reply(400, f"unknown channel {channel!r}; known: {','.join(channels) or '(none)'}")
        d_count, d_delay, d_tier = channels[channel]
        count = str(payload.get("count", d_count)).strip() or d_count
        delay = str(payload.get("delay", d_delay)).strip() or d_delay
        tier = str(payload.get("tier", d_tier)).strip() or d_tier
        # defence-in-depth: keep the shell args tame even though they're whitelisted
        for label, val in (("count", count), ("delay", delay)):
            if not val.replace("m", "").replace("h", "").replace("d", "").replace("s", "").isdigit():
                return self._reply(400, f"bad {label} {val!r}")
        if not all(c.isalnum() or c in ".-_" for c in tier):
            return self._reply(400, f"bad tier {tier!r}")

        # Fire-and-forget: a run can take hours, far past any HTTP deadline.
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        logf = open(LOG, "a")
        subprocess.Popen(
            ["/usr/bin/env", "bash", RUN_DAILY, channel, count, delay, tier],
            cwd=REPO, stdout=logf, stderr=logf, start_new_session=True,
        )
        return self._reply(202, f"accepted: channel={channel} count={count} delay={delay} tier={tier}")


def main():
    port = int(os.environ.get("SCHEDULER_TRIGGER_PORT", "8090"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    sys.stderr.write(f"[trigger] listening on :{port}  (repo={REPO})\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
