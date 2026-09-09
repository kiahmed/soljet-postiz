#!/usr/bin/env python3
"""Pub/Sub emulator helpers for the Simmer poster e2e.

Usage (PUBSUB_EMULATOR_HOST must point at the running emulator):
  emu.py setup   [--project marketresearch-agents] [--topic facades.ticker-events]
  emu.py publish --product simmer --symbol MSTR --state ready --expiry 2026-09-19
  emu.py purge

`setup` creates the topic and a per-product subscription
`simmer-poster-sub` with the filter  attributes.product = "simmer"
(the real design's per-product isolation), plus `all-events-sub` (no filter)
so `purge`/inspection can see everything.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid

from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import pubsub_v1

PROJECT = os.environ.get("GCP_PROJECT", "marketresearch-agents")
TOPIC = os.environ.get("SIMMER_PUBSUB_TOPIC", "facades.ticker-events")
SUB = os.environ.get("SIMMER_PUBSUB_SUBSCRIPTION", "simmer-poster-sub")


def _paths():
    pub = pubsub_v1.PublisherClient()
    sub = pubsub_v1.SubscriberClient()
    return pub, sub, pub.topic_path(PROJECT, TOPIC), sub.subscription_path(PROJECT, SUB)


def setup() -> None:
    pub, sub, tp, sp = _paths()
    try:
        pub.create_topic(name=tp); print(f"topic  + {tp}")
    except AlreadyExists:
        print(f"topic  = {tp}")
    try:
        sub.create_subscription(request={
            "name": sp, "topic": tp, "ack_deadline_seconds": 60,
            "filter": 'attributes.product = "simmer"',
        })
        print(f"sub    + {sp}   filter=attributes.product=\"simmer\"")
    except AlreadyExists:
        print(f"sub    = {sp}")
    allp = sub.subscription_path(PROJECT, "all-events-sub")
    try:
        sub.create_subscription(request={"name": allp, "topic": tp, "ack_deadline_seconds": 60})
        print(f"sub    + {allp}   (no filter)")
    except AlreadyExists:
        pass


def publish(args) -> None:
    pub, _, tp, _ = _paths()
    attrs = {"product": args.product, "symbol": args.symbol, "state": args.state,
             "expiry": args.expiry or "", "score": str(args.score or ""),
             "event_id": args.event_id or f"evt-{uuid.uuid4().hex[:12]}"}
    body = json.dumps({"symbol": args.symbol, "state": args.state,
                       "expiry": args.expiry, "score": args.score,
                       "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}).encode()
    fut = pub.publish(tp, body, **attrs)
    print(f"published {fut.result(timeout=10)}  attrs={attrs}")


def purge() -> None:
    _, sub, _, sp = _paths()
    for name in (sp, sub.subscription_path(PROJECT, "all-events-sub")):
        try:
            sub.seek(request={"subscription": name, "time": {"seconds": int(time.time())}})
            print(f"purged {name}")
        except NotFound:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("setup")
    sp.add_parser("purge")
    p = sp.add_parser("publish")
    p.add_argument("--product", default="simmer")
    p.add_argument("--symbol", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--expiry", default="")
    p.add_argument("--score", default="")
    p.add_argument("--event-id", default="")
    a = ap.parse_args()
    if a.cmd == "setup":
        setup()
    elif a.cmd == "purge":
        purge()
    else:
        publish(a)


if __name__ == "__main__":
    main()
