"""Entity-impact graph imagery for catalyst-anchored posts.

Output is a 1200x675 image showing:
  - the **primary entity** the catalyst is about (centered, sector-colored),
  - its **direct impact** edges (2-4 entities; sentiment +/-/? + optional
    quantitative tag like "$400M" or "+2%"),
  - **indirect / second-order** edges (1-3 entities) below them.

This is what makes Arboryx posts informative — the reader sees the
ecosystem move around the event, not a stock photo. Replaces the simple
branded text card whenever the LLM can extract a real graph from the
post's text + structured context.

Pipeline:
  1. `extract_graph(text, ctx)` — LLM call (OpenAI → Gemini fallback) that
     returns a `GraphSpec` or None.
  2. `render_graph(spec, palette, out_path)` — PIL render of the spec to a
     branded JPEG. Returns False if rendering can't produce a usable file
     (so callers fall through to the next strategy).

LLM cost is ~$0.0002 per extraction (gpt-4o-mini / Gemini Flash Lite).
Output is cached by callers via a hash of (text, ctx).
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .llm import chat as llm_chat
from .watermark import apply_watermark


SENTIMENT_COLOR = {
    "+": "#2BB673",
    "-": "#E84545",
    "?": "#9AA0A6",
    "":  "#9AA0A6",
}
TEXT_COLOR = "#F5F7FA"
DIM_COLOR = "#A8B0BD"


@dataclass
class GraphNode:
    name: str
    sentiment: str = "?"   # '+' | '-' | '?'
    note: str = ""         # short quantitative tag (≤14 chars), e.g. "$400M", "+2%"


@dataclass
class GraphSpec:
    primary: str
    event: str = ""
    sector: str = ""
    direct: list[GraphNode] = field(default_factory=list)
    indirect: list[GraphNode] = field(default_factory=list)


# ---------- LLM extraction ----------

_EXTRACT_SYS = (
    "You analyze deep-tech catalyst posts and extract their impact graph as "
    "STRICT JSON. Output ONLY valid JSON, no prose, no code fences.\n"
    "Schema:\n"
    "{\n"
    '  "primary": "<≤32 chars: the main entity / company / project>",\n'
    '  "event":   "<≤24 chars: what happened, e.g. \\"Series C $400M\\" or \\"FAA Part-450\\">",\n'
    '  "direct":   [{"name":"<≤24 chars>","sentiment":"+|-|?","note":"<≤14 chars or empty>"}],\n'
    '  "indirect": [{"name":"<≤24 chars>","sentiment":"+|-|?","note":"<≤14 chars or empty>"}]\n'
    "}\n"
    "direct = 2-4 entities the catalyst directly affects (suppliers, "
    "customers, competitors, the regulator's target). "
    "indirect = 1-3 second-order effects (downstream pricing, adjacent "
    "sectors, related labor markets). "
    "sentiment: + = beneficiary, - = headwind, ? = ambiguous. "
    "note: quantitative tag if the post has one ($, %, bps, x). Empty if none. "
    "If the post is too vague to extract a meaningful graph, return: "
    '{"primary":"","event":"","direct":[],"indirect":[]}'
)


def extract_graph(text: str, ctx: dict) -> GraphSpec | None:
    """Returns a GraphSpec, or None if extraction fails / yields nothing useful."""
    user_msg = (
        f"Sector: {ctx.get('sector') or 'unknown'}\n"
        f"Source URL: {ctx.get('source_url') or 'none'}\n"
        f"Title: {ctx.get('title') or ''}\n"
        f"Subtitle: {ctx.get('subtitle') or ''}\n"
        f"Post text:\n{text[:1500]}"
    )
    raw = llm_chat(_EXTRACT_SYS, user_msg, max_tokens=600)
    if not raw:
        return None
    # Strip accidental code fences
    cleaned = re.sub(r"^\s*```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find a JSON object inside the response
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    primary = (data.get("primary") or "").strip()
    if not primary:
        return None

    def _coerce_node(n: dict) -> GraphNode | None:
        name = (n.get("name") or "").strip()
        if not name:
            return None
        sentiment = (n.get("sentiment") or "?").strip()
        if sentiment not in {"+", "-", "?"}:
            sentiment = "?"
        return GraphNode(name=name[:30], sentiment=sentiment, note=(n.get("note") or "").strip()[:18])

    direct = [g for g in (_coerce_node(n) for n in (data.get("direct") or [])[:4]) if g]
    indirect = [g for g in (_coerce_node(n) for n in (data.get("indirect") or [])[:3]) if g]

    # If we have nothing but a primary, the graph isn't worth rendering.
    if not direct and not indirect:
        return None

    return GraphSpec(
        primary=primary[:40],
        event=(data.get("event") or "").strip()[:30],
        sector=ctx.get("sector") or "",
        direct=direct,
        indirect=indirect,
    )


# ---------- Render ----------

def render_graph(spec: GraphSpec, palette: tuple[str, str], out_path: Path) -> bool:
    """Render the spec to a 1200x675 JPEG at out_path. Returns True on success."""
    try:
        bg, accent = palette
        W, H = 1200, 675
        img = Image.new("RGB", (W, H), bg)
        draw = ImageDraw.Draw(img)

        # Left accent band
        draw.rectangle([0, 0, 14, H], fill=accent)

        fonts = _load_fonts()
        margin = 60

        # Sector tag (top-left)
        if spec.sector:
            draw.text((margin, 32), spec.sector.upper(), fill=accent, font=fonts["tag"])

        # Primary node
        primary_w = 560
        primary_h = 96
        primary_x = (W - primary_w) // 2
        primary_y = 96
        _draw_primary(draw, primary_x, primary_y, primary_w, primary_h, accent, spec, fonts)

        # Direct row — connect from bottom of primary
        edge_origin = (primary_x + primary_w // 2, primary_y + primary_h)
        if spec.direct:
            direct_y = primary_y + primary_h + 90
            _draw_row(draw, spec.direct, direct_y, W, fonts, edge_origin,
                      bold_label=True, small=False)

            # Indirect row — connect from below the direct row centerline
            if spec.indirect:
                indirect_y = direct_y + 130
                indirect_origin = (W // 2, direct_y + 78)
                _draw_row(draw, spec.indirect, indirect_y, W, fonts, indirect_origin,
                          bold_label=False, small=True)
        elif spec.indirect:
            indirect_y = primary_y + primary_h + 90
            _draw_row(draw, spec.indirect, indirect_y, W, fonts, edge_origin,
                      bold_label=False, small=True)

        # Footer brand
        footer = "arboryx.ai"
        fb = draw.textbbox((0, 0), footer, font=fonts["foot"])
        draw.text((W - margin - (fb[2] - fb[0]), H - margin), footer, fill=DIM_COLOR, font=fonts["foot"])

        out_path.parent.mkdir(parents=True, exist_ok=True)
        apply_watermark(img).save(out_path, "JPEG", quality=90, optimize=True, progressive=True)
        return out_path.exists() and out_path.stat().st_size > 5000
    except Exception:
        if os.getenv("IMAGERY_DEBUG", "false").lower() == "true":
            print("[imagery_graph] render failed:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
        return False


def _draw_primary(draw, x, y, w, h, accent, spec, fonts):
    _rounded_rect(draw, x, y, x + w, y + h, radius=14, fill=accent, outline=accent, width=2)
    # Primary name (top), event (bottom)
    name = _truncate_to_fit(draw, spec.primary, fonts["primary"], w - 28)
    nb = draw.textbbox((0, 0), name, font=fonts["primary"])
    nw = nb[2] - nb[0]
    if spec.event:
        draw.text((x + (w - nw) // 2, y + 18), name, fill="#FFFFFF", font=fonts["primary"])
        ev = _truncate_to_fit(draw, spec.event, fonts["event"], w - 28)
        eb = draw.textbbox((0, 0), ev, font=fonts["event"])
        ew = eb[2] - eb[0]
        draw.text((x + (w - ew) // 2, y + 56), ev, fill="#E8EDF5", font=fonts["event"])
    else:
        nh = nb[3] - nb[1]
        draw.text((x + (w - nw) // 2, y + (h - nh) // 2 - 4), name, fill="#FFFFFF", font=fonts["primary"])


def _draw_row(draw, nodes, y, canvas_w, fonts, edge_origin, *, bold_label, small):
    n = len(nodes)
    spacing = canvas_w // (n + 1)
    box_h = 64 if not small else 52
    box_w = min(220, spacing - 30)
    name_font = fonts["node"] if not small else fonts["node_sm"]
    note_font = fonts["note"] if not small else fonts["note_sm"]

    for i, node in enumerate(nodes):
        cx = spacing * (i + 1)
        x = cx - box_w // 2
        color = SENTIMENT_COLOR.get(node.sentiment, DIM_COLOR)

        # Edge from origin to node top-center
        draw.line([edge_origin, (cx, y)], fill=color, width=2)
        # Sentiment dot at the node entry
        r = 5
        draw.ellipse([cx - r, y - r, cx + r, y + r], fill=color)

        # Node box
        _rounded_rect(draw, x, y, x + box_w, y + box_h, radius=10,
                      fill=None, outline=color, width=2)

        # Name (centered)
        name = _truncate_to_fit(draw, node.name, name_font, box_w - 16)
        nb = draw.textbbox((0, 0), name, font=name_font)
        nw = nb[2] - nb[0]
        if node.note:
            draw.text((x + (box_w - nw) // 2, y + 8), name, fill=TEXT_COLOR, font=name_font)
            note = _truncate_to_fit(draw, node.note, note_font, box_w - 16)
            nb2 = draw.textbbox((0, 0), note, font=note_font)
            nw2 = nb2[2] - nb2[0]
            draw.text((x + (box_w - nw2) // 2, y + box_h - 28 if not small else y + box_h - 24),
                      note, fill=color, font=note_font)
        else:
            nh = nb[3] - nb[1]
            draw.text((x + (box_w - nw) // 2, y + (box_h - nh) // 2 - 4), name,
                      fill=TEXT_COLOR, font=name_font)


# ---------- helpers ----------

def _rounded_rect(draw, x0, y0, x1, y1, *, radius, fill=None, outline=None, width=1):
    if hasattr(draw, "rounded_rectangle"):
        draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill,
                               outline=outline, width=width)
    else:
        draw.rectangle([x0, y0, x1, y1], fill=fill, outline=outline, width=width)


def _truncate_to_fit(draw, text: str, font, max_w: int) -> str:
    bb = draw.textbbox((0, 0), text, font=font)
    if bb[2] - bb[0] <= max_w:
        return text
    s = text
    while s and (draw.textbbox((0, 0), s + "…", font=font)[2] > max_w):
        s = s[:-1]
    return (s + "…") if s else text[:1]


def _load_fonts():
    candidates_regular = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    candidates_bold = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    reg = next((c for c in candidates_regular if Path(c).exists()), None)
    bold = next((c for c in candidates_bold if Path(c).exists()), reg)
    if not reg:
        d = ImageFont.load_default()
        return {k: d for k in ["tag", "primary", "event", "node", "note", "node_sm", "note_sm", "foot"]}
    return {
        "tag":     ImageFont.truetype(reg,  22),
        "primary": ImageFont.truetype(bold, 30),
        "event":   ImageFont.truetype(reg,  22),
        "node":    ImageFont.truetype(bold, 19),
        "note":    ImageFont.truetype(reg,  17),
        "node_sm": ImageFont.truetype(bold, 16),
        "note_sm": ImageFont.truetype(reg,  14),
        "foot":    ImageFont.truetype(reg,  22),
    }
