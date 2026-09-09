"""Recipe layer: ways to compose a post bundle from different inputs.

Each recipe returns a PostBundle that the dispatcher can push to Postiz.
Adding a new recipe = one new function here + one CLI sub-arg.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .composer import _llm_rewrite, _read_context, compose_catalyst, compose_finding
from .config_loader import Tier
from .funnel import append_link_to_text, deep_link_for
from .sources.factory import build_source, first_firestore


@dataclass
class PostBundle:
    text: str
    source_type: str          # firestore | duckdb | narrative | digest | event
    source_id: str            # doc id, slug, or synthetic key for tracking
    media_paths: list[Path] = field(default_factory=list)
    # If set, dispatch as a thread with these exact parts (skips auto-split).
    # If None, the dispatcher auto-splits `text` when it exceeds X's char limit.
    parts: list[str] | None = None
    # Free-form metadata for downstream tooling (auto-imagery, alt text, etc).
    # Common keys: source_url, sector, title, subtitle, headlines, link.
    context: dict = field(default_factory=dict)


def _budget_for_link(link: str, total: int = 280, separator_chars: int = 2) -> int:
    """Char budget for the LLM rewrite that leaves room for an appended deep
    link (URL + blank-line separator). X counts every URL as 23 chars via t.co
    regardless of length, but raw len is what the splitter measures locally —
    so reserve raw len + separator. Falls back to `total` when no link."""
    if not link:
        return total
    return max(60, total - len(link) - separator_chars)


# ---------- frontmatter parsing (tiny, no PyYAML dependency) ----------

def _frontmatter(md: str) -> tuple[dict, str]:
    if not md.startswith("---"):
        return {}, md
    end = md.find("\n---", 3)
    if end < 0:
        return {}, md
    fm: dict[str, str] = {}
    for line in md[3:end].strip().splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm, md[end + 4:].lstrip()


# ---------- recipes ----------

def recipe_single(tier: Tier, source_id: str) -> PostBundle:
    """One Firestore finding (parent), KG card (branch via cards.json), or
    KG catalyst (branch via DuckDB) → one post."""
    for ds in tier.sources:
        try:
            src = build_source(ds, tier)
            item = src.get(source_id)
        except (KeyError, ValueError):
            continue

        if ds.type in ("firestore", "firestore_inherited"):
            link = deep_link_for(tier, "firestore", item) or ""
            budget = _budget_for_link(link)
            ctx = {
                "source_url": item.get("source_url") or "",
                "sector": item.get("category") or "",
                "title": (item.get("finding") or "")[:120],
                "subtitle": item.get("category") or "",
            }
            if link:
                ctx["deep_link"] = link
            text = compose_finding(tier, item, max_chars=budget)
            text = append_link_to_text(text, link)
            return PostBundle(
                text=text,
                source_type="firestore",
                source_id=source_id,
                context=ctx,
            )

        if ds.type == "simmer_api":
            return _simmer_bundle(tier, item)

        if ds.type in ("cards_json", "firestore_cards"):
            related = src.get_related(source_id)
            link = deep_link_for(tier, "cards_json", item) or ""
            budget = _budget_for_link(link)
            ctx = {
                "source_url": "",
                "sector": tier.raw.get("SECTORS", "").split(",")[0].strip()
                          or item.get("sector") or "",
                "title": (item.get("headline") or "")[:120],
                "subtitle": item.get("subtitle") or "",
                "card": item,  # full card dict — imagery layer will map it
            }
            if link:
                ctx["deep_link"] = link
            text = compose_catalyst(tier, item, related, max_chars=budget)
            text = append_link_to_text(text, link)
            return PostBundle(
                text=text,
                source_type=ds.type,   # "cards_json" or "firestore_cards"
                source_id=source_id,
                context=ctx,
            )

        # DuckDB / other branch sources
        related = src.get_related(source_id)
        link = deep_link_for(tier, "duckdb", item) or ""
        budget = _budget_for_link(link)
        ctx = {
            "source_url": item.get("source_url") or item.get("url") or "",
            "sector": item.get("category") or item.get("sector") or "",
            "title": (item.get("title") or item.get("name") or item.get("headline") or "")[:120],
            "subtitle": item.get("summary") or item.get("description") or "",
        }
        if link:
            ctx["deep_link"] = link
        text = compose_catalyst(tier, item, related, max_chars=budget)
        text = append_link_to_text(text, link)
        return PostBundle(
            text=text,
            source_type="duckdb",
            source_id=source_id,
            context=ctx,
        )
    raise KeyError(f"source-id '{source_id}' not found in tier '{tier.id}'")


# ---------- Simmer (Facades) — event-driven, deterministic templating ----------

def _fmt_num(v, nd: int = 2, pct: bool = False, plus: bool = False) -> str | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if pct:
        f = f * 100 if abs(f) <= 1.5 else f
        return f"{f:.0f}%"
    s = f"{f:.{nd}f}"
    return f"+{s}" if plus and f >= 0 else s


def compose_simmer(tier: Tier, card: dict, *, max_chars: int = 260) -> str:
    """Deterministic post text from Simmer engine state. No LLM — the numbers
    ARE the message, and the post must equal the snapshot at publish time.

    Two states: `watch_entered` ("started simmering") and `ready`
    ("ready to serve"). Missing metrics degrade gracefully (watch cards can
    carry nulls before the engine has a full read)."""
    sym = (card.get("symbol") or "").upper()
    st = card.get("state") or "watch_entered"
    m = card.get("metrics") or {}
    expiry = card.get("expiry") or ""
    iv = _fmt_num(m.get("iv_pct"), pct=True)
    vrp = _fmt_num(m.get("vrp"), nd=2)
    em = _fmt_num(m.get("em_1sd"), nd=1)
    news = card.get("sentiment") or {}
    news_score = _fmt_num(news.get("score"), nd=2, plus=True)

    bits: list[str]
    if st == "ready":
        bits = [f"${sym} is ready to serve."]
        if iv or vrp:
            bits.append("IV pct " + (iv or "n/a") + (f", VRP {vrp}" if vrp else "") + ".")
        if em:
            bits.append(f"Short strikes sit outside the GEX wall and the 1-SD move (±{em}).")
        if expiry:
            bits.append(f"Expiry {expiry}.")
        if news_score:
            bits.append(f"News read {news_score}.")
        bits.append("A snapshot of engine state, not advice.")
    else:  # watch_entered / simmering
        bits = [f"${sym} just went on the stove."]
        seg = []
        if iv:
            seg.append(f"IV pct {iv}")
        if vrp:
            seg.append(f"VRP {vrp}")
        if em:
            seg.append(f"expected move ±{em}")
        if seg:
            bits.append(", ".join(seg) + (f" into {expiry}." if expiry else "."))
        bits.append("Watching the gates — we'll say when it's ready.")

    text = " ".join(b for b in bits if b)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _simmer_bundle(tier: Tier, card: dict) -> PostBundle:
    sym = (card.get("symbol") or "").upper()
    link = deep_link_for(tier, "simmer_api", card) or card.get("url") or ""
    text = compose_simmer(tier, card, max_chars=_budget_for_link(link))
    text = append_link_to_text(text, link)
    return PostBundle(
        text=text,
        source_type="simmer_api",
        source_id=card.get("card_id") or card.get("id") or sym,
        context={
            "source_url": card.get("url") or "",
            "sector": "Options income",
            "title": (card.get("headline") or "")[:120],
            "subtitle": f"{sym} · {card.get('state') or ''}",
            "card": card,
            "deep_link": link,
        },
    )


def recipe_simmer(tier: Tier, source_id: str, *, state: str | None = None) -> PostBundle:
    """Simmer post for one ticker state-change. `state` (from the Pub/Sub event:
    watch_entered | ready | …) overrides whatever the API's current decision
    implies, so an event fired on the transition posts the right moment even if
    the engine has moved on by the time we re-pull."""
    src = build_source(tier.sources[0], tier)
    card = src.get(source_id)
    if state:
        card["state"] = state
    return _simmer_bundle(tier, card)


def recipe_narrative(tier: Tier, slug: str, repo_root: Path) -> PostBundle:
    """File-based post: products/<tier>/narratives/<slug>.md.

    Frontmatter keys:
      llm_rewrite: true|false  (default false — narratives are intentional copy)
      media: <repo-relative path>
    """
    f = tier.dir / "narratives" / f"{slug}.md"
    if not f.exists():
        raise FileNotFoundError(f"narrative not found: {f}")
    fm, body = _frontmatter(f.read_text())

    text = body.strip()
    if fm.get("llm_rewrite", "false").lower() == "true":
        text = _llm_rewrite(_read_context(tier), text) or text

    media: list[Path] = []
    if fm.get("media"):
        m = Path(fm["media"])
        media.append((repo_root / m).resolve() if not m.is_absolute() else m)

    return PostBundle(
        text=text,
        source_type="narrative",
        source_id=f"narrative:{tier.id}:{slug}",
        media_paths=media,
    )


def recipe_sector_digest(tier: Tier, sector: str, since: datetime, limit: int = 10) -> PostBundle:
    """Top N findings in a sector since `since` → one summarized post."""
    src = first_firestore(tier)
    if not src:
        raise RuntimeError(f"No Firestore source for tier {tier.id}")
    src.filter_category = sector  # override per call
    items = src.list_recent(since=since, limit=limit)
    if not items:
        raise RuntimeError(f"No findings for sector '{sector}' since {since.date()}")

    bullets = []
    for it in items:
        head = (it.get("finding") or "")[:160]
        url = it.get("source_url") or ""
        bullets.append(f"• {head} {url}".rstrip())

    raw = (
        f"{sector} — {len(items)} catalysts since {since.date().isoformat()}:\n\n"
        + "\n".join(bullets)
    )
    text = _llm_rewrite(_read_context(tier), raw) or raw

    digest_id = f"digest:{sector}:{since.date().isoformat()}"
    headlines = [(it.get("finding") or "")[:90] for it in items[:3]]
    ctx = {
        "sector": sector,
        "title": f"{sector} · {len(items)} catalysts",
        "subtitle": f"since {since.date().isoformat()}",
        "headlines": headlines,
    }
    return PostBundle(text=text, source_type="digest", source_id=digest_id, context=ctx)


def recipe_event(
    tier: Tier,
    *,
    title: str,
    body: str = "",
    link: str = "",
    media: Path | None = None,
    event_key: str | None = None,
    llm_rewrite: bool = True,
) -> PostBundle:
    """Ad-hoc event post: regulation, major industry shift, anything one-off."""
    parts = [title]
    if body:
        parts.append(body)
    if link:
        parts.append(link)
    raw = "\n\n".join(parts)
    text = _llm_rewrite(_read_context(tier), raw) if llm_rewrite else raw
    text = text or raw

    eid = event_key or f"event:{datetime.utcnow().isoformat()}"
    ctx = {
        "title": title[:120],
        "subtitle": (body or "")[:120],
        "source_url": link or "",
    }
    return PostBundle(
        text=text,
        source_type="event",
        source_id=eid,
        media_paths=[media] if media else [],
        context=ctx,
    )
