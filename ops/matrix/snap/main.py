#!/usr/bin/env python3
"""matrix-snap — per-product screenshot Cloud Run service.

POST /snap  {symbol, expiry?, state?, view}  ->  image/png

Multi-view fork of ops/simmer/snap/main.py: Matrix has several post moments,
each with its own crop, instead of Simmer's one. `view` selects both the
render endpoint's query param and the crop selector:

  GET {MATRIX_API_BASE}/matrix/snap/<SYM>?view=<view>
  Authorization: Bearer {MATRIX_API_TOKEN}
  -> standalone HTML card (inline CSS, no SPA, no user session —
     mirrors EdgeLane's app/simmer_snap.py::render_snap_card, see
     docs/simmer.md "Snapshot render endpoint" for the pattern this follows)
  -> screenshot [data-snap="<view>"]

`view` ∈ engine_pick | strategy_grid | bias_chip | walls_chip | win_eval_grid
(docs/matrix_integration.md §Screenshot capture). Falls back to "engine_pick"
if the caller omits it.

As of this writing EdgeLane has not built /matrix/snap/<SYM> yet — every call
here will fail (connection error or 404), and imagery.py's _matrix_snap()
treats that as "no image" and posts text-only rather than losing the post.

Private service: deploy with --no-allow-unauthenticated; callers (matrix-poster)
present a Google OIDC identity token.

Env: MATRIX_API_BASE (https://edge.facades.trade), MATRIX_API_TOKEN,
     SNAP_VIEWPORT (1200x900), SNAP_TIMEOUT_MS (15000), PORT (8080).
"""
from __future__ import annotations

import os
import time

from flask import Flask, request, Response, jsonify

API_BASE = os.environ.get("MATRIX_API_BASE", "https://edge.facades.trade").rstrip("/")
# .strip(): Secret Manager values created via a shell heredoc/echo often carry
# a trailing newline, which breaks Playwright's set_extra_http_headers ("Invalid
# header value") — this bit us on simmer-snap, strip defensively here too.
API_TOKEN = os.environ.get("MATRIX_API_TOKEN", "").strip()
VIEWPORT = os.environ.get("SNAP_VIEWPORT", "1200x900")
TIMEOUT_MS = int(os.environ.get("SNAP_TIMEOUT_MS", "15000"))
DEFAULT_VIEW = "engine_pick"
VALID_VIEWS = {"engine_pick", "strategy_grid", "bias_chip", "walls_chip", "win_eval_grid"}

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
    view = str(body.get("view") or DEFAULT_VIEW).strip()
    if view not in VALID_VIEWS:
        return jsonify({"error": f"view must be one of {sorted(VALID_VIEWS)}, got {view!r}"}), 400
    selector = f'[data-snap="{view}"]'
    url = f"{API_BASE}/matrix/snap/{symbol}?view={view}"
    vw, vh = (int(x) for x in VIEWPORT.split("x"))
    t0 = time.time()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": vw, "height": vh}, device_scale_factor=2)
            if API_TOKEN:
                page.set_extra_http_headers({"Authorization": f"Bearer {API_TOKEN}"})
            resp = page.goto(url, wait_until="networkidle", timeout=TIMEOUT_MS)
            # A 404/500 from the render endpoint is still a "successful"
            # navigation as far as Playwright is concerned — goto() doesn't
            # raise on it. Left unchecked, the selector-wait below times out
            # and falls through to the full-viewport fallback, which happily
            # screenshots the upstream's {"detail":"Not Found"} error page and
            # posts THAT as the image. Fail loudly instead: no image beats a
            # screenshot of an error page.
            if resp is not None and not resp.ok:
                browser.close()
                return jsonify({"error": f"upstream {resp.status}", "url": url}), 502
            try:
                el = page.wait_for_selector(selector, timeout=TIMEOUT_MS, state="visible")
                png = el.screenshot(type="png")
            except Exception:
                # The page loaded fine (status check above passed) but the
                # named crop's data-snap wrapper wasn't found — fall back to a
                # full-viewport shot of that same good page so a selector
                # drift still yields *an* image rather than failing the post.
                png = page.screenshot(type="png", full_page=False)
            browser.close()
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)[:400], "url": url}), 502
    return Response(png, 200, {
        "Content-Type": "image/png",
        "X-Snap-Url": url,
        "X-Snap-View": view,
        "X-Snap-Ms": str(int((time.time() - t0) * 1000)),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
