#!/usr/bin/env python3
"""Simmer (Facades) publisher — the event-driven counterpart to bin/daily.py.

The Simmer engine publishes ticker state-change events to a GCP Pub/Sub topic
(one subscription per product, filter attributes.product="simmer"). This process
consumes them and posts to the `simmer` tier's channels via the Postiz public
API. It terminates on Cloud Run — no work comes back to this machine; all data
retrieval is over HTTP (EdgeLane read-only API + the simmer-snap screenshot
Cloud Run).

Event shape (Pub/Sub message attributes, body is a compact snapshot):
    product   "simmer"
    symbol    "MSTR"
    state     watch_entered | ready | score_crossed | dropped | expiry_near
    expiry    "2026-08-28"
    score     "0.71"
    event_id  "<engine-unique>"

Modes:
    --serve            HTTP server for Pub/Sub PUSH delivery  (Cloud Run default)
    --pull [--max N]   pull-drain the subscription once       (local / catch-up)
    --event '<json>'   process one inline event               (tests, no Pub/Sub)

Safety: --mode defaults to `draft` (posts land unpublished in Postiz for review).
Pass --mode now to actually publish. --dry-run composes + snaps but never calls
Postiz.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from bin._common import integration_ids_for, load_dotenv  # noqa: E402
from src.lib.channel_dispatch import channel_label, channel_parts  # noqa: E402
from src.lib.config_loader import load_tier  # noqa: E402
from src.lib.imagery import auto_media  # noqa: E402
from src.lib.postiz_client import PostizClient  # noqa: E402
from src.lib.recipes import recipe_simmer  # noqa: E402
from src.lib.sources.simmer_source import make_card_id  # noqa: E402
from src.lib.thread import split_for_thread  # noqa: E402
from src.lib import posted_log  # noqa: E402

TIER_ID = "simmer"


def _log(event: str, **kw) -> None:
    print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                      "event": event, **kw}, default=str), flush=True)


def _as_obj(resp):
    """Postiz endpoints return either a dict or a single-element list — normalise
    to a dict so `.get(...)` is always safe."""
    if isinstance(resp, list):
        return resp[0] if resp and isinstance(resp[0], dict) else {}
    return resp if isinstance(resp, dict) else {}


# --------------------------------------------------------------------- dedupe
class Dedupe:
    """Idempotency guard. Firestore when GCP is reachable, else a local JSON
    file — either way one post per (event_id | symbol+day+state)."""

    def __init__(self, tier):
        self.project = (tier.raw.get("SIMMER_PUBSUB_PROJECT")
                        or os.getenv("GCP_PROJECT") or "").strip()
        self.coll = os.getenv("SIMMER_DEDUPE_COLLECTION", "simmer_poster_dedupe")
        self._fs = None
        if self.project and os.getenv("SIMMER_DEDUPE_BACKEND", "auto") != "local":
            try:
                from google.cloud import firestore
                self._fs = firestore.Client(project=self.project)
            except Exception as e:  # noqa: BLE001
                _log("dedupe_firestore_unavailable", error=str(e))
        self._path = REPO_ROOT / "data" / "simmer_poster_dedupe.json"

    @staticmethod
    def key(evt: dict) -> str:
        if evt.get("event_id"):
            return str(evt["event_id"])
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        return f"{(evt.get('symbol') or '').upper()}-{day}-{evt.get('state') or ''}"

    def seen(self, k: str) -> bool:
        if self._fs is not None:
            try:
                return self._fs.collection(self.coll).document(k).get().exists
            except Exception as e:  # noqa: BLE001
                _log("dedupe_read_failed", error=str(e))
                return False
        try:
            return k in set(json.loads(self._path.read_text()))
        except (OSError, json.JSONDecodeError):
            return False

    def mark(self, k: str, meta: dict) -> None:
        if self._fs is not None:
            try:
                self._fs.collection(self.coll).document(k).set(
                    {**meta, "at": datetime.now(timezone.utc).isoformat()})
                return
            except Exception as e:  # noqa: BLE001
                _log("dedupe_write_failed", error=str(e))
        try:
            cur = set(json.loads(self._path.read_text()))
        except (OSError, json.JSONDecodeError):
            cur = set()
        cur.add(k)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(sorted(cur)))


# ------------------------------------------------------------------- event I/O
def normalize_event(raw: dict) -> dict:
    """Accept either a bare event dict or a Pub/Sub PUSH envelope
    {message:{data:<b64 json>, attributes:{...}}}."""
    msg = raw.get("message") if isinstance(raw, dict) else None
    if isinstance(msg, dict):
        attrs = dict(msg.get("attributes") or {})
        body = {}
        if msg.get("data"):
            try:
                body = json.loads(base64.b64decode(msg["data"]).decode("utf-8"))
            except Exception:  # noqa: BLE001
                body = {}
        merged = {**body, **attrs}          # attributes win (they're the routing keys)
        merged.setdefault("event_id", msg.get("messageId") or msg.get("message_id"))
        return merged
    return dict(raw)


# --------------------------------------------------------------------- publish
def process_event(raw_evt: dict, *, tier, dedupe: Dedupe,
                  mode: str = "draft", dry_run: bool = False) -> dict:
    evt = normalize_event(raw_evt)
    product = (evt.get("product") or "").lower()
    state = (evt.get("state") or "").lower()
    symbol = (evt.get("symbol") or "").upper()
    result = {"symbol": symbol, "state": state, "status": "skipped"}

    if product and product != TIER_ID:
        result["reason"] = f"product={product!r}"
        return result
    allowed = {s.strip() for s in (tier.raw.get("POST_ON_STATES") or "").split(",") if s.strip()}
    if allowed and state not in allowed:
        result["reason"] = f"state {state!r} not in POST_ON_STATES {sorted(allowed)}"
        return result
    if not symbol:
        result["status"] = "error"
        result["reason"] = "no symbol"
        return result

    k = dedupe.key(evt)
    if dedupe.seen(k):
        result["status"] = "duplicate"
        result["key"] = k
        return result

    card_id = make_card_id(symbol, datetime.now(timezone.utc).isoformat(), evt.get("expiry") or "")
    try:
        bundle = recipe_simmer(tier, card_id, state=state,
                               symbol=symbol, expiry=evt.get("expiry") or "")
    except Exception as e:  # noqa: BLE001
        result["status"] = "error"
        result["reason"] = f"compose: {e}"
        return result
    if (bundle.context or {}).get("card", {}).get("_enrich") == "minimal":
        _log("enrich_minimal", symbol=symbol, state=state,
             note="read-only API had no card — posting from event attributes only")
        result["enrich"] = "minimal"

    parts = bundle.parts or split_for_thread(bundle.text)
    media_paths = auto_media(tier, bundle, "single")
    iids = integration_ids_for(tier)
    result.update(card_id=card_id, text=bundle.text,
                  media=[str(p) for p in media_paths],
                  channels=[channel_label(tier, i) for i in iids])

    if not iids:
        result["status"] = "no-channels"
        result["reason"] = "integration_ids_for(simmer) is empty (LINKEDIN_ENABLED? X id set?)"
        return result

    if dry_run:
        result["status"] = "dry-run"
        ec = None
        rendered = {}
        for i in iids:
            lbl = channel_label(tier, i)
            ch_parts, ec = channel_parts(tier, lbl, source_type="simmer_api",
                                         source_id=card_id, parts=parts, entities_cache=ec)
            rendered[lbl] = "\n\n".join(ch_parts)
        result["rendered"] = rendered
        return result

    client = PostizClient(api_key=os.environ.get("POSTIZ_API_KEY"))
    media: list[dict] = []
    for m in media_paths:
        if not Path(m).exists():
            continue
        try:
            up = _as_obj(client.upload(Path(m)))
            if up.get("id") and up.get("path"):
                media.append({"id": up["id"], "path": up["path"]})
        except Exception as e:  # noqa: BLE001
            _log("upload_failed", media=str(m), error=str(e))

    ec = None
    posted_channels = []
    for iid in iids:
        lbl = channel_label(tier, iid)
        ch_parts, ec = channel_parts(tier, lbl, source_type="simmer_api",
                                     source_id=card_id, parts=parts, entities_cache=ec)
        try:
            resp = client.create_post(parts=ch_parts, integration_ids=[iid],
                                      mode=mode, media=media or None)
            pid = _as_obj(resp).get("id") or _as_obj(resp).get("postId")
            posted_channels.append({"channel": lbl,
                                    "state": "PUBLISHED" if mode == "now" else "DRAFT",
                                    "post_id": pid})
            _log("posted", symbol=symbol, state=state, channel=lbl, mode=mode, post_id=pid)
        except Exception as e:  # noqa: BLE001
            posted_channels.append({"channel": lbl, "state": "ERROR", "error": str(e)[:300]})
            _log("post_failed", symbol=symbol, channel=lbl, error=str(e)[:300])

    ok = [c for c in posted_channels if c["state"] in ("PUBLISHED", "DRAFT")]
    if ok:
        posted_log.mark_posted(source_type="simmer_api", source_id=card_id, tier=TIER_ID,
                               mode=mode, text=bundle.text,
                               integration_ids=[i for i in iids],
                               response={"channels": posted_channels})
        dedupe.mark(k, {"symbol": symbol, "state": state, "card_id": card_id, "mode": mode})
    result["status"] = "posted" if ok else "error"
    result["result_channels"] = posted_channels
    return result


# ----------------------------------------------------------------------- modes
def run_pull(tier, dedupe, *, mode, dry_run, max_msgs, timeout, sub=None):
    """Drain the subscription once (local / catch-up). Synchronous unary pull:
    on an EMPTY subscription the RPC blocks server-side and eventually surfaces
    DeadlineExceeded — that just means "nothing to pull", not an error, so it
    exits 0 with processed=0. A message whose processing errors is left UNACKED
    so Pub/Sub redelivers it (mirrors the push handler's 500)."""
    sub = (sub or tier.raw.get("SIMMER_PUBSUB_SUBSCRIPTION")
           or os.getenv("SIMMER_PUBSUB_SUBSCRIPTION") or "").strip()
    project = (tier.raw.get("SIMMER_PUBSUB_PROJECT") or os.getenv("GCP_PROJECT") or "").strip()
    if not sub or not project:
        _log("pull_misconfigured", subscription=sub, project=project)
        return 2

    from google.api_core import exceptions as gexc
    from google.cloud import pubsub_v1

    client = pubsub_v1.SubscriberClient()
    path = client.subscription_path(project, sub)
    _log("pull_start", subscription=path, max=max_msgs, budget_s=timeout)

    got = errors = 0
    deadline = time.time() + timeout
    while got < max_msgs and time.time() < deadline:
        rpc_timeout = max(2.0, min(10.0, deadline - time.time()))
        try:
            resp = client.pull(
                request={"subscription": path,
                         "max_messages": min(10, max_msgs - got)},
                timeout=rpc_timeout,
                retry=None,          # don't let the client burn the whole budget retrying
            )
        except gexc.NotFound:
            _log("pull_no_such_subscription", subscription=path)
            return 2
        except (gexc.DeadlineExceeded, gexc.RetryError, gexc.ServiceUnavailable):
            break                    # empty / quiet subscription — normal
        except Exception as e:       # noqa: BLE001
            _log("pull_rpc_error", error=str(e)[:300])
            return 3

        if not resp.received_messages:
            break

        ack, nack = [], []
        for rm in resp.received_messages:
            env = {"message": {"data": base64.b64encode(rm.message.data).decode(),
                               "attributes": dict(rm.message.attributes),
                               "messageId": rm.message.message_id}}
            r = process_event(env, tier=tier, dedupe=dedupe, mode=mode, dry_run=dry_run)
            _log("processed", **r)
            got += 1
            if r.get("status") == "error":
                errors += 1
                nack.append(rm.ack_id)
            else:
                ack.append(rm.ack_id)
        if ack:
            client.acknowledge(request={"subscription": path, "ack_ids": ack})
        if nack:                     # 0s deadline => immediate redelivery
            client.modify_ack_deadline(request={"subscription": path,
                                                "ack_ids": nack, "ack_deadline_seconds": 0})

    _log("pull_done", processed=got, errors=errors)
    return 1 if errors else 0


def build_app(tier, dedupe, *, mode, dry_run):
    """The Flask app for Pub/Sub PUSH delivery. Used by gunicorn (create_app,
    the Cloud Run entrypoint) and by `--serve` (local)."""
    from flask import Flask, jsonify, request
    app = Flask(__name__)

    @app.get("/healthz")
    def health():  # noqa: ANN202
        return "ok", 200

    @app.post("/")
    def push():  # noqa: ANN202
        r = process_event(request.get_json(force=True, silent=True) or {},
                          tier=tier, dedupe=dedupe, mode=mode, dry_run=dry_run)
        _log("processed", **r)
        # 2xx = ack the message; a transient error should 5xx so Pub/Sub retries.
        code = 500 if r.get("status") == "error" else 204
        return (jsonify(r), 200) if code == 200 else ("", code)

    return app


def create_app():
    """gunicorn app factory:  gunicorn 'bin.simmer_poster:create_app()'
    Config comes from the environment (Cloud Run --set-env-vars / --set-secrets):
    SIMMER_POSTER_MODE (default 'now'), SIMMER_POSTER_DRY_RUN."""
    load_dotenv()
    tier = load_tier(TIER_ID)
    dedupe = Dedupe(tier)
    mode = os.environ.get("SIMMER_POSTER_MODE", "now")
    dry_run = os.environ.get("SIMMER_POSTER_DRY_RUN", "").lower() in ("1", "true", "yes")
    _log("serve_start", entrypoint="gunicorn", mode=mode, dry_run=dry_run)
    return build_app(tier, dedupe, mode=mode, dry_run=dry_run)


def run_serve(tier, dedupe, *, mode, dry_run, port):
    _log("serve_start", entrypoint="flask", port=port, mode=mode, dry_run=dry_run)
    build_app(tier, dedupe, mode=mode, dry_run=dry_run).run(host="0.0.0.0", port=port)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--serve", action="store_true", help="HTTP server for Pub/Sub push (Cloud Run)")
    g.add_argument("--pull", action="store_true", help="pull-drain the subscription once")
    g.add_argument("--event", help="process one inline event (JSON string or @file)")
    ap.add_argument("--mode", choices=["draft", "schedule", "now"], default="draft",
                    help="Postiz post mode (default: draft — safe)")
    ap.add_argument("--dry-run", action="store_true", help="compose + snap, never call Postiz")
    ap.add_argument("--max", type=int, default=50, help="--pull: max messages")
    ap.add_argument("--timeout", type=int, default=30, help="--pull: seconds")
    ap.add_argument("--sub", help="--pull: subscription name override "
                    "(else SIMMER_PUBSUB_SUBSCRIPTION / tier.config)")
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    args = ap.parse_args()

    load_dotenv()
    tier = load_tier(TIER_ID)
    dedupe = Dedupe(tier)

    if args.event:
        payload = args.event
        if payload.startswith("@"):
            payload = Path(payload[1:]).read_text()
        r = process_event(json.loads(payload), tier=tier, dedupe=dedupe,
                          mode=args.mode, dry_run=args.dry_run)
        print(json.dumps(r, indent=2, default=str))
        return 0 if r.get("status") in ("posted", "dry-run", "duplicate", "skipped") else 1

    if args.pull:
        return run_pull(tier, dedupe, mode=args.mode, dry_run=args.dry_run,
                        max_msgs=args.max, timeout=args.timeout, sub=args.sub)

    # default: serve
    run_serve(tier, dedupe, mode=args.mode, dry_run=args.dry_run, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
