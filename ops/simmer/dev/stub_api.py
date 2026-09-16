#!/usr/bin/env python3
"""Dev stub for EdgeLane's Simmer read-only API — enough for the poster e2e.

  GET /simmer/ready?since=&limit=            -> [{symbol, expiry, state, score, ...}]
  GET /simmer/state/<SYM>?block=<block>      -> one block, or the full card
      block ∈ card | score | gates | sentiment | evolution

Bearer auth: Authorization: Bearer $SIMMER_API_TOKEN  (default 'dev-simmer-token').
Card data is derived from EdgeLane/simmer_demo_<sym>_*.json when present, else
synthesised. Run:  python ops/simmer/dev/stub_api.py [--port 8899]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

TOKEN = os.environ.get("SIMMER_API_TOKEN", "dev-simmer-token")
EDGELANE = os.environ.get("EDGELANE_DIR", "/mnt/c/soljet_dev/EdgeLane")
# Symbols the stub presents as "ready" even if the EdgeLane demo dump is vetoed —
# so the ready feed + preview path have something to chew on.
FORCE_READY = {s.strip().upper() for s in
               os.environ.get("STUB_FORCE_READY", "MSTR").split(",") if s.strip()}

# symbol -> engine `env` block (from the EdgeLane demo dumps, or a synthetic one)
_FIXTURES: dict[str, dict] = {}


def _load_fixtures() -> None:
    for path in glob.glob(os.path.join(EDGELANE, "simmer_demo_*_*.json")):
        try:
            d = json.load(open(path))
        except Exception:
            continue
        env = d.get("env") if isinstance(d, dict) else None
        if isinstance(env, dict) and env.get("symbol"):
            _FIXTURES[env["symbol"].upper()] = d
    # Always have these two for a deterministic e2e.
    _FIXTURES.setdefault("MSTR", _synth("MSTR", "ready"))
    _FIXTURES.setdefault("TSLA", _synth("TSLA", "watch_entered"))


def _synth(sym: str, state: str) -> dict:
    ready = state == "ready"
    return {"demo": True, "expiration": "2026-09-19", "dte": 11, "env": {
        "symbol": sym, "expiration": "2026-09-19", "dte": 11.0, "spot": 231.4,
        "engine_version": "simmer-engine-1.0.0",
        "decision": "ok" if ready else "watching",
        "score": 0.71 if ready else 0.32, "confidence": 0.66,
        "regime": {"state": "contango"},
        "structure": {"kind": "bull_put", "short": 210, "long": 200} if ready else None,
        "strikes": {"short": 210, "long": 200} if ready else None,
        "credit_mid": 1.35 if ready else None,
        "veto_reasons": [] if ready else ["bull_put:liquidity:spread_pct_of_credit"],
        "data_quality": {"news": 1.0, "walls": 1.0, "quote": 1.0},
        "metrics": {"iv_percentile_effective": 68.0, "vrp": 1.32, "em_1sd": 12.4,
                    "em_2sd": 24.8, "max_pain": 220.0, "rr25_put_minus_call": -0.08,
                    "term_slope": 1.03},
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }}


def _card(sym: str, state: str | None = None) -> dict:
    raw = _FIXTURES.get(sym.upper()) or _synth(sym, state or "watch_entered")
    env = dict(raw.get("env") or {})
    decision = env.get("decision") or ""
    st = state or ("ready" if decision in ("ok", "ready", "pass") else "watch_entered")
    if not state and sym.upper() in FORCE_READY:
        st = "ready"
        env["decision"] = "ok"
        env["veto_reasons"] = []
        env.setdefault("structure", {"kind": "bull_put", "short": 210, "long": 200})
    out = dict(raw)
    out["env"] = env
    out["state"] = st
    out["symbol"] = sym.upper()
    out["sentiment"] = {
        "score": 0.41 if st == "ready" else -0.12,
        "velocity_pph": 3.5, "catalyst_lockout": False,
        "headline_count_24h": 22,
    }
    return out


def _block(sym: str, block: str) -> dict:
    c = _card(sym)
    env = c.get("env") or {}
    if block == "card":
        return c
    if block == "score":
        return {"symbol": sym.upper(), "score": env.get("score"),
                "confidence": env.get("confidence"), "decision": env.get("decision"),
                "state": c["state"]}
    if block == "gates":
        return {"symbol": sym.upper(), "veto_reasons": env.get("veto_reasons") or [],
                "clear": not (env.get("veto_reasons") or [])}
    if block == "sentiment":
        return {"symbol": sym.upper(), **c["sentiment"]}
    if block == "evolution":
        base = env.get("score") or 0.3
        return {"symbol": sym.upper(), "series": [
            {"t": f"2026-09-0{i}", "score": round(base - 0.05 * (5 - i), 3)} for i in range(1, 6)]}
    return {"error": f"unknown block {block!r}"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quieter
        pass

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/healthz":               # unauthenticated — liveness only
            return self._send(200, {"ok": True})
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(401, {"error": "bad bearer"})
        if u.path == "/simmer/ready":
            limit = int((q.get("limit") or ["50"])[0])
            ready = []
            for sym, raw in _FIXTURES.items():
                c = _card(sym)
                if c["state"] != "ready":
                    continue
                env = c.get("env") or {}
                ready.append({"symbol": sym, "expiry": env.get("expiration"),
                              "state": "ready", "score": env.get("score"),
                              "computed_at": env.get("computed_at")})
            return self._send(200, {"ready": ready[:limit]})
        m = re.match(r"/simmer/state/([A-Za-z.\-]+)$", u.path)
        if m:
            block = (q.get("block") or ["card"])[0]
            return self._send(200, _block(m.group(1), block))
        self._send(404, {"error": "no route", "path": u.path})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("STUB_API_PORT", "8899")))
    args = ap.parse_args()
    _load_fixtures()
    print(f"[stub_api] :{args.port}  tickers={sorted(_FIXTURES)}  token={TOKEN!r}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), H).serve_forever()


if __name__ == "__main__":
    main()
