#!/usr/bin/env python3
"""simmer-snap — per-product screenshot Cloud Run service.

POST /snap  {symbol, expiry?, state?}  ->  image/png

Loads  {SIMMER_API_BASE}/simmer/snap/<SYM>  in headless Chromium and
screenshots the crop element (SNAP_SELECTOR, default [data-snap="card"]).
That endpoint is a dedicated, server-rendered standalone HTML card (EdgeLane's
`app/simmer_snap.py::render_snap_card`, inline CSS, no SPA) — NOT the live
`simmer.facades.trade` dashboard, which sits behind a user-login session a
headless browser doesn't have and would only ever screenshot the sign-in
dialog. See EdgeLane's `docs/simmer.md` › "Snapshot render endpoint
(simmer-snap)" for the authoritative contract.

Auth: the endpoint requires `Authorization: Bearer {SIMMER_API_TOKEN}` — the
same read-only token the poster uses against the Simmer API (Secret Manager
`simmer-api-token`), set as a header before navigating (a plain page-open
carries no token).

Private service: deploy with --no-allow-unauthenticated; callers (simmer-poster)
present a Google OIDC identity token.

Env: SIMMER_API_BASE (https://edge.facades.trade), SIMMER_API_TOKEN,
     SNAP_SELECTOR ([data-snap="card"]), SNAP_VIEWPORT (1200x900),
     SNAP_TIMEOUT_MS (15000), PORT (8080).
"""
from __future__ import annotations

import os
import time

from flask import Flask, request, Response, jsonify

API_BASE = os.environ.get("SIMMER_API_BASE", "https://edge.facades.trade").rstrip("/")
# .strip(): Secret Manager values created via `printf ... | gcloud secrets
# create --data-file=-` from a shell heredoc/echo often carry a trailing
# newline; Playwright's set_extra_http_headers rejects any header value
# containing one ("Invalid header value"), so strip defensively rather than
# rely on every secret being created byte-perfect.
API_TOKEN = os.environ.get("SIMMER_API_TOKEN", "").strip()
SELECTOR = os.environ.get("SNAP_SELECTOR", '[data-snap="card"]')
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
    url = f"{API_BASE}/simmer/snap/{symbol}"
    vw, vh = (int(x) for x in VIEWPORT.split("x"))
    t0 = time.time()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": vw, "height": vh}, device_scale_factor=2)
            if API_TOKEN:
                page.set_extra_http_headers({"Authorization": f"Bearer {API_TOKEN}"})
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
