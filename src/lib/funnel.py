"""Deep-link funnel — turn each catalyst-anchored post into traffic.

Every tier-1 (parent) and tier-2 (branch) post that anchors on a specific
finding/card embeds the destination URL in the post text. X auto-renders a
clickable link card from the destination's og:image — better funnel than
attaching a static image (which has no click target).

Per-tier templates:
  Parent (`products/arboryx.ai/tier.config`):
    PARENT_URL_TEMPLATE="https://arboryx.ai/new_growth.html?sector={sector}&entry={entry_id}&date={date}"

  Branch (`products/arboryx.ai/branches/<name>/tier.config`):
    KG_CARD_URL_TEMPLATE="https://robotics.arboryx.ai/?card={card_id}"

Available substitutions are passed in by the recipe via `vars`. Unused vars
are ignored. URL-encoding is applied to each substituted value.
"""
from __future__ import annotations

import os
from urllib.parse import quote_plus

from .config_loader import Tier


def _format(template: str, vars: dict) -> str | None:
    """Safely format `template` with `vars`. Returns None on missing key."""
    if not template:
        return None
    encoded = {k: quote_plus(str(v)) if v is not None else "" for k, v in vars.items()}
    try:
        return template.format(**encoded)
    except (KeyError, IndexError):
        return None


def deep_link_for(tier: Tier, source_type: str, source_data: dict) -> str | None:
    """Build the funnel URL for a post anchored on a specific source row.

    `source_type` is one of: firestore | duckdb | cards_json | narrative | digest | event.
    Returns None if the tier has no template configured or required vars are
    missing from `source_data`.
    """
    # Tier-2 (branch) — cards.json
    if source_type == "cards_json":
        tmpl = tier.raw.get("KG_CARD_URL_TEMPLATE")
        return _format(tmpl, {
            "card_id": source_data.get("card_id") or "",
            "source_id": source_data.get("card_id") or "",
        })

    # Tier-1 (parent) — Firestore findings
    if source_type == "firestore":
        tmpl = tier.raw.get("PARENT_URL_TEMPLATE")
        date_str = (source_data.get("timestamp") or "")[:10]  # ISO → YYYY-MM-DD
        return _format(tmpl, {
            "sector": source_data.get("category") or "",
            "entry_id": source_data.get("entry_id") or "",
            "date": date_str,
        })

    # DuckDB rows from KG (legacy / non-cards.json branch sources): mirror
    # parent template if branch defines KG_CARD_URL_TEMPLATE — caller passes
    # whatever IDs the row has.
    if source_type == "duckdb":
        tmpl = tier.raw.get("KG_CARD_URL_TEMPLATE")
        return _format(tmpl, {
            "source_id": source_data.get("id") or source_data.get("card_id") or "",
            "card_id": source_data.get("id") or source_data.get("card_id") or "",
        })

    return None


def let_x_render_link_card(tier: Tier) -> bool:
    """Whether the imagery ladder should SKIP attaching its image when a deep
    link is present, so X auto-renders the destination's link card instead.

    Default: true. Override per-tier via `LET_X_RENDER_LINK_CARD=false` to
    keep attaching our generated image (e.g., when destination doesn't yet
    expose a per-entry og:image). Branch tier inherits parent's value when
    not set explicitly.
    """
    val = tier.raw.get("LET_X_RENDER_LINK_CARD")
    if val is None and tier.parent_id:
        from .config_loader import load_tier
        val = load_tier(tier.parent_id).raw.get("LET_X_RENDER_LINK_CARD")
    if val is None:
        return True
    return val.lower() == "true"


def append_link_to_text(text: str, link: str) -> str:
    """Append the deep link to the post text (idempotent)."""
    if not link:
        return text
    if link in text:
        return text
    sep = "\n\n" if not text.endswith("\n") else ""
    return f"{text}{sep}{link}"


def branch_enabled(parent_tier: Tier, branch_short_name: str) -> bool:
    """Check the parent's `BRANCH_<NAME>_ENABLED` gate. Mirrors the
    `SECTOR_<NAME>_ENABLED` pattern in arboryx-admin/frontend/arboryx_frontend.config.
    Returns True when the flag is missing (default-on, parent's config decides).
    """
    flag = f"BRANCH_{branch_short_name.upper().replace(' ', '_').replace('&', '').replace('__', '_')}_ENABLED"
    val = parent_tier.raw.get(flag)
    if val is None:
        return True
    return val.lower() == "true"


def branch_short_name(tier: Tier) -> str:
    """Extract the suffix after the parent dot — 'arboryx.robotics' → 'robotics'."""
    if "." not in tier.id:
        return tier.id
    return tier.id.split(".", 1)[1]
