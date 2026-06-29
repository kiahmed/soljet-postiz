"""Apply a diagonal repeating watermark to any image we generate or render.

Used by every imagery strategy that produces a file we paid for OR rendered
ourselves: entity graphs (`imagery_graph.render_graph`), branded text cards
(`imagery._branded_card`), and Gemini-generated photos (`imagery._generate_image`).
NOT applied to user-supplied media (banner.png, etc.) or to KG screenshots —
those originate elsewhere and shouldn't be re-stamped.

Subtle by default — visible enough that a naive crop leaves obvious damage,
light enough not to fight with the content. Tunable via env:
  WATERMARK_DISABLE=true    skip entirely
  WATERMARK_TEXT="arboryx.ai"
  WATERMARK_ALPHA=0.05      0.0 - 1.0 white-on-content opacity
  WATERMARK_FONT_SIZE=72
  WATERMARK_ANGLE=-30       rotation in degrees
  WATERMARK_GAP_X=240       horizontal gap *between repetitions* in px
  WATERMARK_GAP_Y=170       vertical gap between rows
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


_FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def apply_watermark(img: Image.Image) -> Image.Image:
    """Return a watermarked RGB copy of `img`. RGBA in → RGB out (flattened)."""
    if os.getenv("WATERMARK_DISABLE", "false").lower() == "true":
        return img.convert("RGB") if img.mode != "RGB" else img

    text = os.getenv("WATERMARK_TEXT", "arboryx.ai")
    alpha = float(os.getenv("WATERMARK_ALPHA", "0.05"))
    font_size = int(os.getenv("WATERMARK_FONT_SIZE", "72"))
    angle = float(os.getenv("WATERMARK_ANGLE", "-30"))
    gap_x = int(os.getenv("WATERMARK_GAP_X", "240"))
    gap_y = int(os.getenv("WATERMARK_GAP_Y", "170"))

    base = img.convert("RGBA") if img.mode != "RGBA" else img.copy()
    W, H = base.size

    # Oversized canvas so the rotation has no empty corners after cropping back.
    diag = int(math.hypot(W, H)) + 200
    strip = Image.new("RGBA", (diag, diag), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(strip)

    font = _load_font(font_size)
    bb = sdraw.textbbox((0, 0), text, font=font)
    tw = bb[2] - bb[0]
    th = bb[3] - bb[1]
    step_x = tw + gap_x
    step_y = th + gap_y
    fill = (255, 255, 255, max(0, min(255, int(255 * alpha))))

    # Stagger every other row by half a step so the diagonal pattern reads cleanly.
    for row, y in enumerate(range(-th, diag, step_y)):
        offx = (step_x // 2) if row % 2 else 0
        for x in range(-tw + offx, diag, step_x):
            sdraw.text((x, y), text, fill=fill, font=font)

    rotated = strip.rotate(angle, resample=Image.BICUBIC, expand=False)
    rx = (rotated.size[0] - W) // 2
    ry = (rotated.size[1] - H) // 2
    cropped = rotated.crop((rx, ry, rx + W, ry + H))

    return Image.alpha_composite(base, cropped).convert("RGB")


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_BOLD_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()
