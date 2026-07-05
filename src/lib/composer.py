"""Compose a draft from tier context + a payload (finding or catalyst).

Flow:
  1. Build a deterministic template from the source item.
  2. Send the template + composed context.md through the unified `llm.chat`
     (OpenAI primary; Gemini Flash Lite fallback when OpenAI fails).
  3. If both providers fail, return the deterministic template draft.
"""
from __future__ import annotations

import os
import re

from .config_loader import Tier, context_chain
from .llm import chat as llm_chat


def _read_context(tier: Tier) -> str:
    parts = []
    for p in context_chain(tier):
        rel = p.relative_to(p.parents[3]) if len(p.parents) > 3 else p.name
        parts.append(f"# === {rel} ===\n{p.read_text()}")
    purpose = tier.raw.get("POSTING_PURPOSE", "")
    if purpose:
        parts.append(f"# === posting purpose ({tier.id}) ===\n{purpose}")
    return "\n\n".join(parts)


def _llm_rewrite(system_text: str, draft: str, *, max_chars: int = 280) -> str | None:
    """Voice-aligned rewrite via the unified LLM router (OpenAI → Gemini fallback)."""
    user_prompt = (
        f"Rewrite the draft below in the voice from the system context. "
        f"Keep it under ~{max_chars} characters (X-friendly). "
        f"Preserve URLs and tickers verbatim. Do not add hashtags I did not write. "
        f"No emoji unless the draft already has them. Output the rewritten post only — "
        f"no preamble, no quotes around it.\n\n"
        f"DRAFT:\n{draft}"
    )
    return llm_chat(system_text, user_prompt, max_tokens=512)


# ---------- hashtags ----------
# We add 2-3 relevant hashtags deterministically (never via the LLM, which
# hallucinates tags) to attract the right audience and rank in social search.
# Named entities (companies) are the most specific/valuable tags; the per-sector
# list below reliably tops us up to 2-3 when an item has few taggable entities.
HASHTAG_TARGET = 3
_HASHTAG_RESERVE = 32  # chars kept back from the LLM budget so tags fit

SECTOR_HASHTAGS = {
    "robotics": ["#Robotics", "#Automation", "#Humanoids"],
    "crypto": ["#Crypto", "#Web3", "#Blockchain"],
    "ai stack": ["#AI", "#AIInfra", "#MachineLearning"],
    "space & defense": ["#Space", "#Defense", "#Aerospace"],
    "power & energy": ["#Energy", "#CleanEnergy", "#Power"],
    "strategic minerals": ["#CriticalMinerals", "#Mining", "#SupplyChain"],
}
_COMPANY_TYPES = {"public_company", "private_company", "company"}


def _camel_tag(name: str) -> str:
    """'Figure AI' -> '#FigureAI'; drops non-alphanumerics."""
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    return "#" + "".join(w[:1].upper() + w[1:] for w in words) if words else ""


def _relevant_hashtags(sector: str, entities: list[dict] | None, n: int = HASHTAG_TARGET) -> list[str]:
    """Up to n tags: named company entities first (most specific), then the
    sector's curated tags. Deduped case-insensitively, order preserved."""
    tags: list[str] = []
    for e in (entities or []):
        if e.get("type") in _COMPANY_TYPES and e.get("name"):
            t = _camel_tag(e["name"])
            if t:
                tags.append(t)
        if len(tags) >= 2:  # cap entity tags so a sector tag still fits
            break
    sec = (sector or "").strip().lower()
    tags += SECTOR_HASHTAGS.get(sec) or ([f"#{re.sub(r'[^A-Za-z0-9]', '', sector)}"] if sector else [])
    seen, out = set(), []
    for t in tags:
        k = t.lower()
        if t and t != "#" and k not in seen:
            seen.add(k)
            out.append(t)
    return out[:n]


def _append_hashtags(text: str, tags: list[str], max_chars: int) -> str:
    """Append each tag not already present, as long as it fits within max_chars."""
    out = text.rstrip()
    low = out.lower()
    for t in tags:
        if t.lower() in low:
            continue
        candidate = f"{out} {t}"
        if len(candidate) > max_chars:
            continue
        out, low = candidate, candidate.lower()
    return out


def _sector_for(tier: Tier, item: dict) -> str:
    """Sector name for tagging: explicit on the item, else the branch's own name."""
    return (item.get("sector") or item.get("category")
            or (tier.id.split(".")[-1] if tier.parent_id else "")) or ""


def _parse_sentiment_takeaways(s: str) -> dict:
    """Parses the strategist's persisted shape:
        'Sentiment: <label> | Direct: <line> | Indirect: <line> | Market Dynamics: <line>'
    Returns dict with sentiment/direct/indirect/market_dynamics (any may be missing).
    """
    out: dict[str, str] = {}
    if not s:
        return out
    for part in s.split("|"):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        key = k.strip().lower().replace(" ", "_")
        out[key] = v.strip()
    return out


def compose_finding(tier: Tier, finding: dict, *, max_chars: int = 280) -> str:
    """Parent-tier composition from a Firestore `findings` doc.

    Schema (per ../arboryx.ai/values.yaml strategist persistence):
      finding, timestamp, source_url, category, sentiment_takeaways,
      guidance_play, price_levels, tooltip, _hash, entry_id, _synced_at
    """
    head = finding.get("finding") or finding.get("title") or "<no finding>"
    parts = _parse_sentiment_takeaways(finding.get("sentiment_takeaways", ""))
    direct = parts.get("direct", "")
    indirect = parts.get("indirect", "")
    play = finding.get("guidance_play") or ""

    lines = [head]
    if direct:
        lines.append(f"\n→ {direct}")
    if indirect:
        lines.append(f"  ↳ {indirect}")
    if play:
        lines.append(f"\nPlay: {play}")
    draft = "".join(lines)

    body_budget = max(120, max_chars - _HASHTAG_RESERVE)  # leave room for tags
    rewritten = _llm_rewrite(_read_context(tier), draft, max_chars=body_budget) or draft[:body_budget]
    tags = _relevant_hashtags(_sector_for(tier, finding), finding.get("entities"))
    return _append_hashtags(rewritten, tags, max_chars)


def compose_catalyst(tier: Tier, catalyst: dict, related: list[dict], *, max_chars: int = 280) -> str:
    """Branch-tier composition from a KG catalyst row + its related rows."""
    title = catalyst.get("title") or catalyst.get("name") or catalyst.get("finding") or "<catalyst>"
    summary = catalyst.get("summary") or catalyst.get("description") or ""

    rel_names = []
    for r in related[:3]:
        n = r.get("target_name") or r.get("name") or r.get("target_id") or r.get("target")
        if n:
            rel_names.append(str(n))

    lines = [title]
    if summary:
        lines.append(f"\n{summary}")
    if rel_names:
        lines.append(f"\nLinked: {', '.join(rel_names)}")
    draft = "".join(lines)

    body_budget = max(120, max_chars - _HASHTAG_RESERVE)  # leave room for tags
    rewritten = _llm_rewrite(_read_context(tier), draft, max_chars=body_budget) or draft[:body_budget]
    tags = _relevant_hashtags(_sector_for(tier, catalyst), catalyst.get("entities"))
    return _append_hashtags(rewritten, tags, max_chars)
