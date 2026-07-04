"""Compose a draft from tier context + a payload (finding or catalyst).

Flow:
  1. Build a deterministic template from the source item.
  2. Send the template + composed context.md through the unified `llm.chat`
     (OpenAI primary; Gemini Flash Lite fallback when OpenAI fails).
  3. If both providers fail, return the deterministic template draft.
"""
from __future__ import annotations

import os

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
    sector = finding.get("category") or ""

    lines = [head]
    if direct:
        lines.append(f"\n→ {direct}")
    if indirect:
        lines.append(f"  ↳ {indirect}")
    if play:
        lines.append(f"\nPlay: {play}")
    if sector:
        lines.append(f"\n#{sector.replace(' ', '').replace('&', '')}")
    draft = "".join(lines)

    rewritten = _llm_rewrite(_read_context(tier), draft, max_chars=max_chars)
    return rewritten or draft[:max_chars]


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

    rewritten = _llm_rewrite(_read_context(tier), draft, max_chars=max_chars)
    return rewritten or draft[:max_chars]
