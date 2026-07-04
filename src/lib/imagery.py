"""Auto-imagery — decide and produce a post image automatically.

Strategy ladder (first hit wins):

1. **Explicit media** — frontmatter `media:` or `--media` flag from the user.
   Never overridden.

2. **KG card screenshot** — when `tier.config` sets `KG_CARD_URL_TEMPLATE`
   (e.g. `https://robotics.arboryx.ai/cards/{source_id}`) and Playwright is
   installed. Captures the rendered card from the branch's frontend. Best
   for catalyst-anchored posts.

3. **Branded PIL card** — for `sector-digest` and `event` recipes (and any
   `single` post where we infer a sector). Renders a 1200×675 JPEG with
   sector color band, headline, sector tag, and `arboryx.ai` footer.

4. **Skip-because-X-renders-link-card** — if the post text contains a non-
   arboryx URL, X already auto-renders the link's og:image as a card on the
   tweet itself. Returning [] here avoids fighting that.

5. **LLM-generated image** (DALL-E 3) — opt-in via `IMAGERY_GENERATE=true`.
   Uses an LLM-derived prompt from the post text. Cached by prompt hash.

6. **No image** — better than a wrong image.

Optionally an LLM router runs ahead of the rules to bump priority based on
the post's content (set `IMAGERY_LLM_ROUTER=true`). The router only reorders
or skips strategies — it doesn't bypass user-supplied media.

All generated/downloaded images cache to `data/imagery_cache/` keyed on
content hashes so repeated calls are free.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw, ImageFont

from .card_to_graph import card_to_graph_spec
from .config_loader import Tier
from .funnel import let_platform_render_link_card
from .imagery_graph import GraphSpec, extract_graph, render_graph
from .llm import chat as llm_chat, generate_image as llm_generate_image
from .recipes import PostBundle
from .watermark import apply_watermark


REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "data" / "imagery_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Sector → (background hex, accent hex). Used for branded cards.
SECTOR_PALETTE: dict[str, tuple[str, str]] = {
    "Robotics":            ("#1F2937", "#5A8DEE"),
    "Crypto":              ("#1A1A2E", "#F4B400"),
    "AI Stack":            ("#1B1538", "#A06FE3"),
    "Space & Defense":     ("#0E1A3A", "#7AA8FF"),
    "Power & Energy":      ("#0F2A1F", "#2BB673"),
    "Strategic Minerals":  ("#2A1A0E", "#C16A3F"),
}
DEFAULT_PALETTE = ("#0F1B2D", "#3F8AF0")
TEXT_COLOR = "#F5F7FA"
SUB_COLOR = "#A8B0BD"

# Domains whose URLs in post text should NOT trigger the "X auto-card"
# skip — they're our own and X won't render a useful card for them.
OWN_DOMAINS = {"arboryx.ai", "robotics.arboryx.ai"}


def auto_media(tier: Tier, bundle: PostBundle, recipe_name: str) -> list[Path]:
    """Pick imagery for a bundle. Returns [] if no good option."""
    # 1. Explicit media wins
    if bundle.media_paths:
        return bundle.media_paths

    ctx = bundle.context or {}

    # 2. Deep-link funnel — when a deep link was injected into the post text
    #    and the tier opts in to LET_PLATFORM_RENDER_LINK_CARD (default true),
    #    skip our image entirely so the platform (X/LinkedIn) auto-renders the
    #    destination's og:image as a clickable link card.
    if ctx.get("deep_link") and let_platform_render_link_card(tier):
        return []

    # Optional LLM router — reorders strategies, doesn't bypass user media
    forced_strategy = _llm_pick_strategy(bundle, recipe_name) if _llm_router_on() else None
    if forced_strategy == "none":
        return []

    # 3. KG card screenshot — content-anchored, beats X's link card
    #    (Playwright; dormant unless KG_CARD_URL_TEMPLATE + Playwright wired)
    kg = _kg_card(tier, bundle.source_id, ctx)
    if kg:
        return [kg]

    # 4. Structured-card → entity graph (NO LLM; cheap path).
    #    For tier-2 catalyst posts where cards.json carries entities + relationships.
    if recipe_name in ("single", "event"):
        struct = _entity_graph_from_card_data(tier, bundle, ctx)
        if struct:
            return [struct]

    # 5. LLM-extracted entity graph — fallback when no structured card found.
    if recipe_name in ("single", "event"):
        graph = _entity_graph(tier, bundle, ctx)
        if graph:
            return [graph]

    # 4. External URL present → let X auto-render the source's link card
    #    (its og:image + headline is more informative than a generic brand card).
    if _has_external_url(bundle.text):
        return []

    # 5. Branded card for digests / events without graph-worthy structure /
    #    sector-tagged singles where the graph extractor declined.
    if recipe_name in ("sector-digest", "event") or _has_sector(ctx, bundle.text):
        card = _branded_card(tier, bundle, ctx)
        if card:
            return [card]

    # 6. LLM-generated (Gemini, default-on; disable with IMAGERY_GENERATE=false).
    #    Only reached when nothing earlier fired — most posts won't get here.
    if os.getenv("IMAGERY_GENERATE", "true").lower() == "true":
        gen = _generate_image(bundle, ctx)
        if gen:
            return [gen]

    # 7. None
    return []


# ---------- Strategy: KG screenshot ----------

def _kg_card(tier: Tier, source_id: str, ctx: dict) -> Path | None:
    template = tier.raw.get("KG_CARD_URL_TEMPLATE", "").strip()
    if not template:
        return None
    try:
        url = template.format(source_id=source_id, **ctx)
    except (KeyError, IndexError):
        return None
    out = CACHE_DIR / f"kg_{_hash(url)}.jpg"
    if out.exists():
        return out
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if _debug():
            print("[imagery] Playwright not installed; KG screenshot skipped", file=sys.stderr)
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1200, "height": 675})
                page.goto(url, wait_until="networkidle", timeout=15000)
                page.screenshot(path=str(out), type="jpeg", quality=88)
            finally:
                browser.close()
        return out
    except Exception as e:
        if _debug():
            print(f"[imagery] KG screenshot failed for {url}: {e}", file=sys.stderr)
        return None


# ---------- Strategy: structured card → entity graph (no LLM) ----------

def _entity_graph_from_card_data(tier: Tier, bundle: PostBundle, ctx: dict) -> Path | None:
    """Render an entity-impact graph from a curated card dict (cards.json
    shape) carried in `ctx['card']`. Skips the LLM extraction step entirely.
    Returns None when no card data is present so the caller can fall through.
    """
    card = ctx.get("card")
    if not isinstance(card, dict):
        return None
    cache_key = _hash(f"struct|{tier.id}|{bundle.source_id}")
    out = CACHE_DIR / f"graph_{cache_key}.jpg"
    if out.exists():
        return out
    sector = ctx.get("sector") or _infer_sector(bundle.text) or ""
    spec = card_to_graph_spec(card, sector=sector)
    if not spec:
        return None
    palette = SECTOR_PALETTE.get(sector, DEFAULT_PALETTE)
    if not render_graph(spec, palette, out):
        return None
    return out


# ---------- Strategy: entity-impact graph (LLM-extracted fallback) ----------

def _entity_graph(tier: Tier, bundle: PostBundle, ctx: dict) -> Path | None:
    """Extract primary + direct/indirect impact entities from the post and
    render them as an edge-drawn graph. Returns None if the LLM can't extract
    a meaningful spec, in which case the caller falls through."""
    cache_key = _hash(f"graph|{bundle.source_id}|{bundle.text[:300]}")
    out = CACHE_DIR / f"graph_{cache_key}.jpg"
    if out.exists():
        return out
    spec = extract_graph(bundle.text, ctx)
    if not spec:
        return None
    sector = ctx.get("sector") or _infer_sector(bundle.text) or _llm_infer_sector(bundle.text) or ""
    palette = SECTOR_PALETTE.get(sector, DEFAULT_PALETTE)
    spec.sector = sector or spec.sector
    if not render_graph(spec, palette, out):
        return None
    return out


# ---------- Strategy: branded PIL card ----------

def _branded_card(tier: Tier, bundle: PostBundle, ctx: dict) -> Path | None:
    sector = (
        ctx.get("sector")
        or _infer_sector(bundle.text)
        or _llm_infer_sector(bundle.text)
        or ""
    )
    bg, accent = SECTOR_PALETTE.get(sector, DEFAULT_PALETTE)

    title = (ctx.get("title") or _first_sentence(bundle.text) or "Arboryx").strip()
    subtitle = (ctx.get("subtitle") or sector or "Arboryx · deep-tech catalysts").strip()
    headlines = ctx.get("headlines") or []

    cache_key = _hash(f"{sector}|{title}|{subtitle}|{'|'.join(headlines)}")
    out = CACHE_DIR / f"card_{cache_key}.jpg"
    if out.exists():
        return out

    W, H = 1200, 675
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    # Left accent band
    band_w = 16
    draw.rectangle([0, 0, band_w, H], fill=accent)

    fonts = _load_fonts()
    margin = 80
    left = band_w + margin
    max_text_w = W - left - margin

    # Sector tag (small caps, top)
    tag = (sector or "ARBORYX").upper()
    draw.text((left, margin), tag, fill=accent, font=fonts["tag"])

    # Headline (wrapped, large)
    head_y = margin + 64
    wrapped = _wrap(title, fonts["head"], max_text_w, draw)
    for line in wrapped[:3]:
        draw.text((left, head_y), line, fill=TEXT_COLOR, font=fonts["head"])
        head_y += 70

    # Subtitle (wrapped, dimmer)
    if subtitle and subtitle != title:
        sub_lines = _wrap(subtitle, fonts["sub"], max_text_w, draw)
        head_y += 12
        for line in sub_lines[:2]:
            draw.text((left, head_y), line, fill=SUB_COLOR, font=fonts["sub"])
            head_y += 36

    # Headlines bullet list (digest only)
    if headlines:
        bullet_y = head_y + 30
        for h in headlines[:3]:
            line = f"·  {h}"[:90]
            draw.text((left, bullet_y), line, fill=SUB_COLOR, font=fonts["body"])
            bullet_y += 36

    # Footer brand
    footer = "arboryx.ai"
    fbbox = draw.textbbox((0, 0), footer, font=fonts["foot"])
    fw = fbbox[2] - fbbox[0]
    draw.text((W - margin - fw, H - margin), footer, fill=SUB_COLOR, font=fonts["foot"])

    apply_watermark(img).save(out, "JPEG", quality=90, optimize=True, progressive=True)
    return out


def _load_fonts() -> dict:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    bold = None
    regular = None
    for c in candidates:
        if Path(c).exists():
            if "Bold" in c and bold is None:
                bold = c
            elif regular is None:
                regular = c
    bold = bold or regular
    regular = regular or bold
    if not regular:
        d = ImageFont.load_default()
        return {"tag": d, "head": d, "sub": d, "body": d, "foot": d}
    try:
        return {
            "tag":  ImageFont.truetype(regular, 22),
            "head": ImageFont.truetype(bold, 56),
            "sub":  ImageFont.truetype(regular, 28),
            "body": ImageFont.truetype(regular, 26),
            "foot": ImageFont.truetype(regular, 24),
        }
    except Exception:
        d = ImageFont.load_default()
        return {"tag": d, "head": d, "sub": d, "body": d, "foot": d}


# ---------- Strategy: LLM-generated image (opt-in) ----------

def _generate_image(bundle: PostBundle, ctx: dict) -> Path | None:
    """Gemini-only generation (gemini-3-pro-image-preview by default).

    The prompt is built from the post's sector + headline so the image is
    contextually relevant. Output is validated by `llm.generate_image`
    (decodable, reasonable size + dimensions); if validation fails the file
    is removed and we return None — never a half-baked card on disk.
    """
    prompt = _make_image_prompt(bundle.text, ctx)
    out = CACHE_DIR / f"gen_{_hash(prompt)}.png"
    if out.exists():
        return out
    if not llm_generate_image(prompt, out):
        return None
    # Re-encode to JPEG and apply watermark — we paid Gemini credits for this
    # bitmap, so it doesn't leave the cache without our brand stamp on it.
    try:
        jpg = out.with_suffix(".jpg")
        with Image.open(out) as img:
            apply_watermark(img.convert("RGB")).save(
                jpg, "JPEG", quality=90, optimize=True, progressive=True
            )
        out.unlink(missing_ok=True)
        return jpg
    except Exception as e:
        if _debug():
            print(f"[imagery] re-encode generated image failed: {e}", file=sys.stderr)
        return out  # PNG fallback (no watermark, but still usable)


def _make_image_prompt(text: str, ctx: dict) -> str:
    sector = ctx.get("sector") or _infer_sector(text) or "deep tech"
    title = ctx.get("title") or _first_sentence(text) or text[:200]
    return (
        f"Editorial cover image for a {sector.lower()} news brief. "
        f"Subject inspired by: '{title[:200]}'. "
        f"Clean, abstract, photographic with subtle depth, muted color palette, "
        f"no text, no logos, no people, no UI elements, 16:9 aspect."
    )


# ---------- Optional LLM router ----------

def _llm_router_on() -> bool:
    return os.getenv("IMAGERY_LLM_ROUTER", "false").lower() == "true"


def _llm_pick_strategy(bundle: PostBundle, recipe_name: str) -> str | None:
    """Pick an imagery strategy via the unified LLM router. Returns one of
    'kg_card' | 'branded_card' | 'skip_url_card' | 'generate' | 'none' | None
    (None = LLM declined; let rules decide)."""
    sys_msg = (
        "You pick the imagery strategy for a social post. "
        "Choose ONE of: kg_card, branded_card, skip_url_card, generate, none. "
        "Return ONLY the chosen strategy as a single word, no punctuation."
    )
    user_msg = (
        f"Recipe: {recipe_name}\n"
        f"Sector: {bundle.context.get('sector') or 'unknown'}\n"
        f"Has external URL: {_has_external_url(bundle.text)}\n"
        f"Text: {bundle.text[:600]}"
    )
    response = llm_chat(sys_msg, user_msg, max_tokens=8)
    if not response:
        return None
    choice = response.strip().lower().split()[0]
    if choice in {"kg_card", "branded_card", "skip_url_card", "generate", "none"}:
        return choice
    return None


# ---------- helpers ----------

def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _debug() -> bool:
    return os.getenv("IMAGERY_DEBUG", "false").lower() == "true"


def _wrap(text: str, font, max_w: int, draw) -> list[str]:
    words = text.split()
    if not words:
        return [text]
    lines, line = [], ""
    for w in words:
        cand = f"{line} {w}".strip()
        bbox = draw.textbbox((0, 0), cand, font=font)
        if bbox[2] - bbox[0] <= max_w:
            line = cand
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def _first_sentence(text: str) -> str:
    m = re.search(r"^.+?[.!?](?:\s|$)", text.strip(), re.DOTALL)
    return (m.group(0) if m else text[:120]).strip()


def _infer_sector(text: str) -> str | None:
    t = text.lower()
    for sector in SECTOR_PALETTE:
        canon = sector.lower()
        compact = sector.replace(" & ", "").replace(" ", "").lower()
        hashtag = "#" + sector.split()[0].lower()
        if canon in t or compact in t or hashtag in t:
            return sector
    return None


_SECTOR_INFER_CACHE: dict[str, str] = {}


def _llm_infer_sector(text: str) -> str | None:
    """Infer the closest Arboryx sector via the unified LLM router when the
    regex misses. Default-on; disable with IMAGERY_LLM_INFER=false. Cost is
    negligible — falls through OpenAI to Gemini Flash Lite if needed.
    """
    if os.getenv("IMAGERY_LLM_INFER", "true").lower() != "true":
        return None
    key = _hash(text[:600])
    if key in _SECTOR_INFER_CACHE:
        return _SECTOR_INFER_CACHE[key] or None
    sectors = list(SECTOR_PALETTE.keys())
    sys_msg = (
        "Classify a deep-tech post into ONE Arboryx sector. "
        f"Return EXACTLY one of: {', '.join(sectors)}, or NONE. "
        "Output only the sector name with nothing else."
    )
    response = llm_chat(sys_msg, text[:600], max_tokens=12)
    if not response:
        _SECTOR_INFER_CACHE[key] = ""
        return None
    choice = response.strip().strip(".").strip()
    match = next((s for s in sectors if s.lower() == choice.lower()), None)
    _SECTOR_INFER_CACHE[key] = match or ""
    return match


def _has_sector(ctx: dict, text: str) -> bool:
    return bool(ctx.get("sector")) or _infer_sector(text) is not None


def _has_external_url(text: str) -> bool:
    for m in re.finditer(r"https?://([^\s)]+)", text):
        host = m.group(1).lower().split("/")[0]
        # strip leading 'www.'
        host = host[4:] if host.startswith("www.") else host
        if host not in OWN_DOMAINS:
            return True
    return False
