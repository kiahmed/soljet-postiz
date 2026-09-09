#!/usr/bin/env python3
"""Dev stub for the per-product `simmer-snap` Cloud Run screenshot service.

  POST /snap   {symbol, expiry, state}  ->  image/png  (a mock board crop)

The real service runs headless Chromium against simmer.facades.trade/?symbol=..&snap=1
and screenshots [data-snap="card"]. This stub just renders a labelled card with
Pillow so the poster's image path (fetch -> cache -> upload -> attach) is
exercised end to end. Run:  python ops/simmer/dev/stub_snap.py [--port 8898]
"""
from __future__ import annotations

import argparse
import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw

BG = (7, 9, 13)
FG = (245, 247, 250)
ACCENT = (63, 138, 240)
SUB = (168, 176, 189)


def render(symbol: str, expiry: str, state: str) -> bytes:
    w, h = 1200, 675
    im = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w, 8], fill=ACCENT)
    d.text((48, 60), "SIMMER", fill=ACCENT)
    d.text((48, 110), f"${symbol.upper()}", fill=FG)
    label = "READY TO SERVE" if state == "ready" else "STARTED SIMMERING"
    d.text((48, 180), label, fill=FG)
    d.text((48, 230), f"expiry {expiry or 'n/a'}", fill=SUB)
    # fake GEX wall bars
    for i, bar in enumerate([120, 200, 90, 260, 150, 300, 110]):
        x = 48 + i * 150
        d.rectangle([x, 620 - bar, x + 90, 620], fill=ACCENT if i == 3 else (40, 52, 74))
    d.text((48, 630), "GEX walls · gates · readiness · news score  (mock crop)", fill=SUB)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        png = render(str(body.get("symbol") or "???"),
                     str(body.get("expiry") or ""), str(body.get("state") or ""))
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png)))
        self.end_headers()
        self.wfile.write(png)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("STUB_SNAP_PORT", "8898")))
    args = ap.parse_args()
    print(f"[stub_snap] :{args.port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), H).serve_forever()


if __name__ == "__main__":
    main()
