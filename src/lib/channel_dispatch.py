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
from .handles import resolve_handle
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
                 parts: list[str], text: str) -> tuple[list[dict], list[Path]]:
    """Media for an 'attach' channel (e.g. LinkedIn): run the imagery ladder in
    force-attach mode — prefers the destination's per-card og:image PNG, falling
    back to the entity graph — then upload to Postiz. Returns (uploaded, local
    paths); the caller deletes the local paths after a successful post. ([], [])
    if nothing resolves (caller degrades to link_card). Never raises."""
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
        return [], []
    out, locals_ = [], []
    for p in paths:
        try:
            up = client.upload(Path(p))
            if up.get("id") and up.get("path"):
                out.append({"id": up["id"], "path": up["path"]})
                locals_.append(Path(p))
        except Exception:  # noqa: BLE001
            pass
    return out, locals_


def channel_media(client, tier, label: str, *, source_type: str, source_id: str,
                  parts: list[str], text: str, base_media: list[dict],
                  attach_cache):
    """Resolve the media list for ONE channel per its imagery policy.

    Returns (media_list, attach_cache). attach_cache (a dict {media, paths} for
    'attach' tiers) memoizes the upload across channels — pass it back in.
    `cleanup_attach(attach_cache)` deletes the downloaded local files after a
    successful post.
    """
    policy = tier.imagery_policy.get(label.lower())
    if policy == "link_card" and deep_link_from_text(text) is None:
        policy = "attach"  # never post a naked link_card with no link
    if policy == "link_card":
        return [], attach_cache
    if policy == "attach":
        if attach_cache is None:
            media, locals_ = attach_media(client, tier, source_type, source_id, parts, text)
            attach_cache = {"media": media, "paths": locals_}
        return (attach_cache.get("media") or []), attach_cache
    return base_media, attach_cache  # legacy single-decision behavior


def cleanup_attach(attach_cache) -> None:
    """Delete the local attach images downloaded for this post (call after a
    successful publish so data/imagery_cache doesn't accumulate one-shot PNGs)."""
    if isinstance(attach_cache, dict):
        for p in attach_cache.get("paths") or []:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


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


def _tier_cfg(tier, key: str, default: str) -> str:
    """Read a tier.config value, walking up the parent chain (like imagery_policy)."""
    t = tier
    while t is not None:
        if key in t.raw and t.raw[key] != "":
            return t.raw[key]
        t = load_tier(t.parent_id) if t.parent_id else None
    return default


def entity_tags(tier, label: str, entities: list) -> list[str]:
    """Per-channel tags for the subject entities, per ENTITY_TAG_MODE:
    prefer_handle (default) | handle_only | cashtag_only | both.
      - @handle: resolved via handles.resolve_handle (card-embedded → endpoint →
        store), gated by HANDLE_INJECTION. Per channel.
      - $TICKER cashtag: gated by CASHTAGS_ENABLED, and ONLY for entities that
        carry a stock ticker (public companies) — never a $ before every name.
    """
    mode = str(_tier_cfg(tier, "ENTITY_TAG_MODE", "prefer_handle")).lower()
    handles_on = str(_tier_cfg(tier, "HANDLE_INJECTION", "false")).lower() == "true"
    cash_on = str(_tier_cfg(tier, "CASHTAGS_ENABLED", "true")).lower() == "true"
    try:
        max_tags = int(_tier_cfg(tier, "MAX_ENTITY_TAGS", "2"))
    except ValueError:
        max_tags = 2

    tags: list[str] = []
    for e in entities:
        if len(tags) >= max_tags:
            break
        h = resolve_handle(e, label, tier=tier) if handles_on else None
        # LinkedIn only renders a REAL mention from the URN token
        # `@[Name](urn:li:organization:<id>)`; a bare `@slug` is dead text. Turn
        # the verified slug into that token via the LinkedIn API. Fail closed:
        # no URN → drop the tag (never emit a broken/dead mention). X keeps @slug.
        if h and label.lower() == "linkedin":
            from .linkedin_urn import to_mention
            h = to_mention(h, e.get("name") if isinstance(e, dict) else None)
        ticker = e.get("ticker") if isinstance(e, dict) else None
        # Cashtags only work for US-style ALPHA tickers ($NVDA, $SHA). Skip
        # numeric/foreign codes like FANUC's TSE 6954 → "$6954" is meaningless.
        cash = (f"${ticker.upper()}"
                if (cash_on and ticker and re.fullmatch(r"[A-Za-z]{1,6}", ticker))
                else None)
        if mode == "handle_only":
            candidates = [h]
        elif mode == "cashtag_only":
            candidates = [cash]
        elif mode == "both":
            candidates = [h, cash]
        else:  # prefer_handle
            candidates = [h or cash]
        for c in candidates:
            if c and c not in tags and len(tags) < max_tags:
                tags.append(c)
    return tags


def channel_parts(tier, label: str, *, source_type: str, source_id: str,
                  parts: list[str], entities_cache):
    """Per-channel post parts: the body is identical across channels; we append
    the channel's entity tags (@handles and/or $cashtags per ENTITY_TAG_MODE) at
    the END OF THE FIRST PARAGRAPH — right after the hashtags, before the forward
    hook + deep link. Returns (parts, entities_cache) — entities_cache memoizes
    the subject-entity lookup across channels."""
    if entities_cache is None:
        entities_cache = _entities_for(tier, source_type, source_id)
    tags = entity_tags(tier, label, entities_cache) if entities_cache else []
    if not tags or not parts:
        return parts, entities_cache
    tag_str = " ".join(tags)
    p0 = parts[0]
    if "\n\n" in p0:                       # after the first paragraph (hashtags)
        head, rest = p0.split("\n\n", 1)
        p0 = f"{head.rstrip()} {tag_str}\n\n{rest}"
    else:
        p0 = f"{p0.rstrip()} {tag_str}"
    return [p0] + list(parts[1:]), entities_cache
