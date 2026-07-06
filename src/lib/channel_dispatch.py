"""Per-channel dispatch helpers shared by bin/daily.py and bin/post.py.

Implements the per-channel imagery policy from docs/design-per-channel-imagery.md:
each channel a post fans out to can attach a different `image[]` (or none),
while the composed text is identical. Content is composed once; only media
differs per Postiz call.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config_loader import Tier, load_tier
from .handles import apply_handles
from .imagery import auto_media
from .recipes import PostBundle
from .sources.factory import build_source

_URL_RE = re.compile(r"https?://\S+")


def channel_label(tier: Tier, iid: str) -> str:
    """Human channel name ("X", "LinkedIn") for an integration id. Branch tiers
    inherit channels from the parent, so walk up the parent chain to resolve."""
    t: Tier | None = tier
    while t is not None:
        raw = t.raw
        if iid == raw.get("CHANNEL_X_PRIMARY"):
            return "X"
        if iid == raw.get("CHANNEL_LINKEDIN"):
            return "LinkedIn"
        for k, v in raw.items():
            if k.startswith("CHANNEL_") and v == iid:
                return k.replace("CHANNEL_", "").replace("_", " ").title()
        t = load_tier(t.parent_id) if t.parent_id else None
    return f"channel:{iid[:8]}"


def deep_link_from_text(text: str) -> str | None:
    """Find the funnel deep link in the post text. Prefer an arboryx.ai URL (the
    funnel destination) over any other URL an LLM rewrite may have slipped in;
    fall back to the last URL, since the deep link is appended last."""
    urls = [u.rstrip(").,") for u in _URL_RE.findall(text or "")]
    for u in urls:
        if "arboryx.ai" in u:
            return u
    return urls[-1] if urls else None


def attach_media(client, tier, source_type: str, source_id: str,
                 parts: list[str], text: str) -> list[dict]:
    """Media for an 'attach' channel (e.g. LinkedIn): run the imagery ladder in
    force-attach mode — prefers the destination's per-card og:image PNG, falling
    back to the entity graph — then upload to Postiz. [] if nothing resolves (the
    caller then degrades that channel to link_card). Never raises."""
    ctx: dict = {}
    dl = deep_link_from_text(text)
    if dl:
        ctx["deep_link"] = dl
    if source_type in ("cards_json", "firestore_cards") and tier.sources:
        try:
            ctx["card"] = build_source(tier.sources[0], tier).get(source_id)
        except Exception:  # noqa: BLE001
            pass
    bundle = PostBundle(text=text, source_type=source_type, source_id=source_id,
                        parts=parts, context=ctx)
    try:
        paths = auto_media(tier, bundle, "single", force_attach=True)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for p in paths:
        try:
            up = client.upload(Path(p))
            if up.get("id") and up.get("path"):
                out.append({"id": up["id"], "path": up["path"]})
        except Exception:  # noqa: BLE001
            pass
    return out


def channel_media(client, tier, label: str, *, source_type: str, source_id: str,
                  parts: list[str], text: str, base_media: list[dict],
                  attach_cache):
    """Resolve the media list for ONE channel per its imagery policy.

    Returns (media_list, attach_cache). attach_cache memoizes the (possibly
    expensive) attach media across channels within one post — pass it back in.
    """
    policy = tier.imagery_policy.get(label.lower())
    if policy == "link_card" and deep_link_from_text(text) is None:
        policy = "attach"  # never post a naked link_card with no link
    if policy == "link_card":
        return [], attach_cache
    if policy == "attach":
        if attach_cache is None:
            attach_cache = attach_media(client, tier, source_type, source_id, parts, text)
        return (attach_cache or []), attach_cache
    return base_media, attach_cache  # legacy single-decision behavior


def tier_has_channel_policy(tier) -> bool:
    return bool(getattr(tier, "imagery_policy", None))


def _entities_for(tier, source_type: str, source_id: str) -> list:
    """Primary subject entities of the item (relationship-weighted), so @mentions
    feature the event's actual subject rather than whichever entity is listed first."""
    if source_type not in ("cards_json", "firestore_cards") or not tier.sources:
        return []
    try:
        from .composer import primary_entities
        card = build_source(tier.sources[0], tier).get(source_id)
        return primary_entities(card)
    except Exception:  # noqa: BLE001
        return []


def channel_parts(tier, label: str, *, source_type: str, source_id: str,
                  parts: list[str], entities_cache):
    """Per-channel post parts: append the channel-correct @handles for the
    item's entities (Figure AI → @figure on LinkedIn, @Figure_robots on X).
    Only the mentions differ per channel; the rest of the copy is identical.
    Returns (parts, entities_cache) — entities_cache memoizes the entity lookup."""
    if entities_cache is None:
        entities_cache = _entities_for(tier, source_type, source_id)
    if not entities_cache or not parts:
        return parts, entities_cache
    return ([apply_handles(parts[0], entities_cache, label)] + list(parts[1:]),
            entities_cache)
