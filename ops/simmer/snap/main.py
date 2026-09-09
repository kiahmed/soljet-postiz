#!/usr/bin/env python3
"""simmer-snap — per-product screenshot Cloud Run service.

POST /snap  {symbol, expiry?, state?}  ->  image/png

Loads  {SIMMER_SITE}/?symbol=<SYM>&snap=1  in headless Chromium, waits for the
board, and screenshots the crop element (SNAP_SELECTOR, default
[data-snap="card"] — the ticker's GEX walls + gate checklist + readiness +
news-sentiment score). The Simmer UI's ?snap=1 mode is expected to:
  - authenticate via SNAP_TOKEN (header X-Snap-Token) or render public snapshot data
  - hide nav / toasts (reuse the existing data-no-capture convention)
  - expose SNAP_SELECTOR wrapping just the board crop
  - set a per-symbol og:image + a click-through <a> to /?symbol=<SYM>

Private service: deploy with --no-allow-unauthenticated; callers (simmer-poster,
the postiz box) present a Google OIDC identity token.

Env: SIMMER_SITE (https://simmer.facades.trade), SNAP_SELECTOR, SNAP_TOKEN,
     SNAP_VIEWPORT (1200x900), SNAP_TIMEOUT_MS (15000), PORT (8080).
"""
from __future__ import annotations

import os
import time

from flask import Flask, request, Response, jsonify

SITE = os.environ.get("SIMMER_SITE", "https://simmer.facades.trade").rstrip("/")
SELECTOR = os.environ.get("SNAP_SELECTOR", '[data-snap="card"]')
TOKEN = os.environ.get("SNAP_TOKEN", "")
VIEWPORT = os.environ.get("SNAP_VIEWPORT", "1200x900")
TIMEOUT_MS = int(os.environ.get("SNAP_TIMEOUT_MS", "15000"))

app = Flask(__name__)


@app.get("/healthz")
def healthz() -> Response:
    return Response("ok", 200)


@app.post("/snap")
def snap() -> Response:
    body = request.get_json(force=True, silent=True) or {}
    symbol = str(body.get("symbol") or "").upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    url = f"{SITE}/?symbol={symbol}&snap=1"
    vw, vh = (int(x) for x in VIEWPORT.split("x"))
    t0 = time.time()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": vw, "height": vh}, device_scale_factor=2)
            if TOKEN:
                page.set_extra_http_headers({"X-Snap-Token": TOKEN})
            page.goto(url, wait_until="networkidle", timeout=TIMEOUT_MS)
            try:
                el = page.wait_for_selector(SELECTOR, timeout=TIMEOUT_MS, state="visible")
                png = el.screenshot(type="png")
            except Exception:
                # fall back to a full-viewport shot so a selector drift still
                # yields *an* image rather than failing the post
                png = page.screenshot(type="png", full_page=False)
            browser.close()
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)[:400], "url": url}), 502
    return Response(png, 200, {
        "Content-Type": "image/png",
        "X-Snap-Url": url,
        "X-Snap-Ms": str(int((time.time() - t0) * 1000)),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
