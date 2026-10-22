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
import re
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from .config_loader import Tier

_URL_RE = re.compile(r"https?://\S+")
_TRAILING = ").,;:!?"


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
    # Facades · Simmer — per-ticker live board
    if source_type == "simmer_api":
        tmpl = tier.raw.get("PARENT_URL_TEMPLATE")
        return _format(tmpl, {
            "symbol": source_data.get("symbol") or "",
            "expiry": source_data.get("expiry") or "",
        })

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


def let_platform_render_link_card(tier: Tier) -> bool:
    """Whether the imagery ladder should SKIP attaching its image when a deep
    link is present, so the PLATFORM (X and LinkedIn both) auto-renders the
    destination's link card from its og:image instead. Applies to every channel
    the post fans out to — imagery is decided once per post, not per platform.

    Default: true. Override per-tier via `LET_PLATFORM_RENDER_LINK_CARD=false`
    to keep attaching our generated image (e.g., when the destination doesn't
    yet expose a per-entry og:image). Branch tier inherits parent's value when
    not set explicitly. The legacy key `LET_X_RENDER_LINK_CARD` is still read
    as a fallback.
    """
    def _read(t: Tier):
        return (t.raw.get("LET_PLATFORM_RENDER_LINK_CARD")
                or t.raw.get("LET_X_RENDER_LINK_CARD"))
    val = _read(tier)
    if val is None and tier.parent_id:
        from .config_loader import load_tier
        val = _read(load_tier(tier.parent_id))
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


def _cfg(tier: Tier, key: str, default: str) -> str:
    """Tier value, walking up the parent chain (branch inherits parent)."""
    t = tier
    while t is not None:
        if t.raw.get(key):
            return t.raw[key]
        if not t.parent_id:
            break
        from .config_loader import load_tier
        t = load_tier(t.parent_id)
    return default


def utm_url(url: str, *, source: str, campaign: str, content: str = "",
            medium: str = "social") -> str:
    """Add utm_* params to `url` so GA attributes the visit to this post.
    Leaves a URL that already carries utm_source untouched (a hand-tagged
    link wins); existing query params and the fragment are kept."""
    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    if any(k == "utm_source" for k, _ in q):
        return url
    q += [("utm_source", source), ("utm_medium", medium), ("utm_campaign", campaign)]
    if content:
        q.append(("utm_content", content))
    return urlunsplit(parts._replace(query=urlencode(q)))


def tag_links(tier: Tier, label: str, text: str, content: str = "") -> str:
    """UTM-tag every link to our own sites in `text` for channel `label`.

    Without this, a click from the LinkedIn/X apps lands in GA as
    "(direct) / (none)" — no way to tell which post, product or channel drove
    it. Applied per channel at fan-out time (channel_parts / reflex with_cta),
    never to the shared composed text, so the og:image lookup and posted-log
    still see the clean URL. X counts any URL as 23 chars (Postiz validates
    with twitter-text, transformedURLLength=23), so the longer URL doesn't
    eat into the 280 budget.

    Tier knobs: UTM_TAGGING="false" turns it off; UTM_DOMAINS is the
    comma list of domains to tag (default arboryx.ai,facades.trade —
    subdomains included). utm_campaign = tier id, utm_source = channel.
    """
    if _cfg(tier, "UTM_TAGGING", "true").lower() != "true" or not text:
        return text
    domains = [d.strip().lower() for d in
               _cfg(tier, "UTM_DOMAINS", "arboryx.ai,facades.trade").split(",") if d.strip()]
    source = label.strip().lower().replace(" ", "_")

    def _sub(m: re.Match) -> str:
        raw = m.group(0)
        url = raw.rstrip(_TRAILING)
        tail = raw[len(url):]
        host = (urlsplit(url).hostname or "").lower()
        if not any(host == d or host.endswith("." + d) for d in domains):
            return raw
        return utm_url(url, source=source, campaign=tier.id, content=content) + tail

    return _URL_RE.sub(_sub, text)
