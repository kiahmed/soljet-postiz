"""Compose a draft from tier context + a payload (finding or catalyst).

Flow:
  1. Build a deterministic template from the source item.
  2. Send the template + composed context.md through the unified `llm.chat`
     (OpenAI primary; Gemini Flash Lite fallback when OpenAI fails).
  3. If both providers fail, return the deterministic template draft.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone

from .card_to_graph import card_to_graph_spec
from .config_loader import Tier, context_chain
from .llm import chat as llm_chat


def _card_age_days(date_str) -> int | None:
    try:
        d = datetime.fromisoformat(str(date_str)[:10]).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).days
    except Exception:  # noqa: BLE001
        return None


def _when_phrase(date_str) -> str | None:
    """'mid May' / 'late March' / 'early June' from a YYYY-MM-DD date."""
    try:
        d = datetime.fromisoformat(str(date_str)[:10])
    except Exception:  # noqa: BLE001
        return None
    bucket = "early" if d.day <= 10 else "mid" if d.day <= 20 else "late"
    return f"{bucket} {d.strftime('%B')}"


# --------------------------------------------------------- relationship hooks
# The forward hook (the line before the deep link) is keyed to the card's
# STRONGEST relationship, so a funding card and a rivalry card don't close with
# the same sentence.
#
# Deliberately ENTITY-FREE. An earlier build named the entities ("X backed Y")
# and an adversarial pass over all 255 live cards found it asserting things the
# data doesn't support: direction inverted against the edge's own `mechanism`
# (UBTECH "built on" its own product), pending deals in the past tense
# ("Onconetix took in Realbotix" — mechanism says "in the process of
# acquiring"), a real startup named off a generic noun ("Japan Airlines is
# trialling Humanoid" from the words "humanoid robots"), and subsidiaries
# swapped for parents. The KG has no deal-completion field and `evidence_type`
# means "reported", not "closed", so those are not fixable by tightening a gate.
# Naming nobody makes that entire class structurally impossible: the hook points
# at the card's graph instead of asserting who did what to whom. It also drops
# the body-mention gate that was suppressing ~31% of cards, so coverage is high
# instead of ~30%.
#
# Each entry: (plain, hedged). Variants are indexed by a stable hash of the
# source id so a backlog run doesn't repeat one line — same card always yields
# the same hook.
_REL_HOOKS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "invests_in": (("Follow the money here ↓", "Who's backing this ↓",
                    "The funding trail on this ↓"),
                   ("How we read the money here ↓",)),
    "acquires": (("The deal behind this ↓", "See who's consolidating ↓",
                  "The M&A angle here ↓"),
                 ("How we read the deal here ↓",)),
    "partners_with": (("See who's teaming up ↓", "The alliance behind this ↓",
                       "Who's joining forces here ↓"),
                      ("How we read the tie-up ↓",)),
    "supplies": (("The supply chain behind this ↓", "See who feeds this stack ↓",
                  "Who's supplying whom here ↓"),
                 ("How we read the supply link ↓",)),
    "deploys": (("See where this is running ↓", "Who's putting it to work ↓",
                 "The deployment picture here ↓"),
                ("How we read the rollout ↓",)),
    "pilots": (("See what's being trialled ↓", "The pilot picture here ↓",
                "Who's testing what ↓"),
               ("How we read the trial ↓",)),
    "built_on": (("See what this is built on ↓", "The stack underneath ↓",
                  "What this stands on ↓"),
                 ("How we read the dependency ↓",)),
    "integrates_with": (("See what's plugging together ↓", "The integration picture ↓",
                         "What's converging here ↓"),
                        ("How we read the integration ↓",)),
    "competes_with": (("See who's up against whom ↓", "The competitive picture here ↓",
                       "Who's fighting for this ↓"),
                      ("How we read the rivalry ↓",)),
    "displaces": (("See what's being displaced ↓", "Who's losing ground here ↓",
                   "The displacement angle ↓"),
                  ("How we read the pressure ↓",)),
    "benchmarks_against": (("See how they measure up ↓", "The benchmark picture ↓",
                            "Who leads on the numbers ↓"),
                           ("How we read the comparison ↓",)),
    "spins_out_from": (("See where this came from ↓", "The spin-out story here ↓",
                        "Tracing its origins ↓"),
                       ("How we read the split ↓",)),
    "regulates": (("The regulatory angle here ↓", "See who sets the rules ↓",
                   "Where policy bites here ↓"),
                  ("How we read the policy angle ↓",)),
    "hires_from": (("See where the talent's moving ↓", "The talent flow here ↓",
                    "Who's hiring from whom ↓"),
                   ("How we read the talent move ↓",)),
}
# 'direct'/'web_grounded' state it plainly; 'inferred'/'speculative' are OUR
# reading of a signal, so they take the hedged column. Rank orders the tie-break
# (better-evidenced edge wins at equal confidence).
_EVIDENCE_RANK = {"direct": 0, "web_grounded": 1, "inferred": 2, "speculative": 3}
_HEDGED_EVIDENCE = {"inferred", "speculative"}


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _top_relationship(card: dict) -> dict | None:
    """The card's strongest relationship, or None. Ranked by confidence, then
    unflagged first, then evidence quality, then impact — ending in the list
    index so ties break identically on every run."""
    rels = card.get("relationships")
    if not isinstance(rels, list):
        return None
    ranked = []
    for i, r in enumerate(rels):
        if not isinstance(r, dict) or r.get("rel") not in _REL_HOOKS:
            continue
        if str(r.get("status", "active")) != "active":
            continue
        ev = str(r.get("evidence_type") or "")
        if ev not in _EVIDENCE_RANK:
            continue
        ranked.append((
            -round(_num(r.get("confidence")), 4),
            1 if r.get("flagged") is True else 0,   # prefer unflagged
            _EVIDENCE_RANK[ev],
            -round(_num(r.get("impact_magnitude")) * _num(r.get("mechanism_strength")), 4),
            i, r))
    if not ranked:
        return None
    ranked.sort(key=lambda t: t[:5])
    return ranked[0][5]


def card_confidence(card: dict) -> float:
    """The card's conviction score = its top relationship's confidence — the
    SAME edge that drives the forward hook, so gate/status/composer agree on
    what a card's headline claim is. 0.0 when there are no usable edges."""
    rel = _top_relationship(card) if isinstance(card, dict) else None
    try:
        return float(rel.get("confidence")) if rel else 0.0
    except (TypeError, ValueError):
        return 0.0


def _mechanism_clause(card: dict | None, max_chars: int = 220) -> str | None:
    """One sentence of 'why' from the card's strongest relationship's own
    `mechanism` field — quoted VERBATIM, never rephrased or re-derived. This
    is deliberate, not a shortcut: the adversarial pass documented above (the
    entity-free-hook comment) found real errors — inverted direction, past
    tense on a pending deal — every time a template tried to RE-SYNTHESIZE a
    sentence from rel/entities/mechanism. Quoting mechanism's own words
    carries none of that risk; it's the exact trust model compose_catalyst()
    already applies to share.linkedin_text/twitter_text (KG-authored prose,
    used as-is). Returns None — never a fabricated filler sentence — when
    there's no usable relationship or no mechanism text; `card_confidence`
    already established `_top_relationship` as the one shared definition of
    "the card's strongest relationship", reused here rather than re-derived."""
    rel = _top_relationship(card) if isinstance(card, dict) else None
    if not rel:
        return None
    mech = str(rel.get("mechanism") or "").strip()
    if not mech:
        return None
    m = re.match(r"(.{1,%d}?[.!?])(\s|$)" % max_chars, mech)
    if m:
        return m.group(1)
    if len(mech) <= max_chars:
        return mech
    clipped = mech[:max_chars].rsplit(" ", 1)[0].rstrip(",;: ")
    return f"{clipped}…" if clipped else None


def _relationship_hook(card: dict | None, source_id: str = "") -> str | None:
    """Entity-free hook for the card's strongest relationship, or None to fall
    back to the generic hook. Never raises — a hook is cosmetic."""
    if not isinstance(card, dict):
        return None
    try:
        rel = _top_relationship(card)
        if rel is None:
            return None
        plain, hedged = _REL_HOOKS[rel["rel"]]
        pool = hedged if str(rel.get("evidence_type")) in _HEDGED_EVIDENCE else plain
        if not pool:
            return None
        # stable per-card variant choice — deterministic, spreads a backlog run
        idx = int(hashlib.sha1(str(source_id).encode()).hexdigest()[:8], 16) % len(pool)
        return pool[idx]
    except Exception:  # noqa: BLE001
        return None


def _temporal_frame(text: str, date_str, *, kind: str = "card",
                    card: dict | None = None, source_id: str = "") -> tuple[str, str]:
    """Deterministic temporal framing shared by both composers. Items older than
    a week open with a 'Back in <when>:' lead-in (so they read as context, not
    filler); returns (lead-in-prefixed text, forward hook to place before the link).

    kind='card' (robotics KG cards) — the link opens a rich per-card page with a
      relationship graph, so the hook promises "how this played out".
    kind='finding' (parent tier) — the link is just a simple entry, nothing to
      visualize, so the hook is toned down to point at RELATED updates in the
      space rather than this catalyst's evolution."""
    age = _card_age_days(date_str)
    recent = age is None or age <= 7
    if not recent:
        when = _when_phrase(date_str)
        if when:
            text = f"Back in {when}: {text}"
    if kind == "finding":
        # Parent tier: a plain entry page, no graph to visualise — stays generic.
        hook = ("See what else is moving in the space ↓" if recent
                else "See what's followed in the space since ↓")
    else:
        hook = _relationship_hook(card, source_id) or (
            "Following how this unfolds ↓" if recent
            else "How it's played out since — tracking it ↓")
    return text, hook


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
        f"Keep it under ~{max_chars} characters (X-friendly).\n"
        f"STRICT GROUNDING — use ONLY the facts, company names, and numbers "
        f"already present in the draft. Do NOT invent or add anything not in the "
        f"draft: no funding amounts, valuations, dollar figures, investors, "
        f"partners, product names, tickers, dates, or claims that aren't there. "
        f"If the draft doesn't state a number, don't state one. "
        f"Preserve any URLs and tickers verbatim; do NOT add new URLs. "
        f"Do not add hashtags I did not write. No emoji unless the draft has them. "
        f"Output the rewritten post only — no preamble, no quotes around it.\n\n"
        f"DRAFT:\n{draft}"
    )
    return llm_chat(system_text, user_prompt, max_tokens=512)


# ---------- hashtags ----------
# We add 2-3 relevant hashtags deterministically (never via the LLM, which
# hallucinates tags) to attract the right audience and rank in social search.
# Named entities (companies) are the most specific/valuable tags; the per-sector
# list below reliably tops us up to 2-3 when an item has few taggable entities.
HASHTAG_TARGET = 3
# CKG's per-card hashtags (topic/theme-aware, e.g. #AgTech, #Partnership) are
# worth more room than our own entity+sector guess — a higher target ONLY
# for that branch of _card_hashtags() so the fallback path (_relevant_
# hashtags, no KG data) is untouched. _append_hashtags()'s per-tag length
# check still drops anything that doesn't fit, so this never forces the
# budget; it just raises the ceiling for the richer source.
KG_HASHTAG_TARGET = 5
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


def _card_hashtags(card: dict, sector: str, entities: list[dict] | None,
                   n: int = HASHTAG_TARGET) -> list[str]:
    """Prefer catalyst-knowledge-graph's own per-catalyst `hashtags` field
    (its src/hashtags.py, docs/graph-posters.md "Per-card hashtags") over our
    own entity+sector derivation — it knows the card's topic (top
    relationship type) and theme keywords from the headline/finding, neither
    of which is available here. Priority-ordered [≤2 entity, 1 topic,
    ≤2 theme, 1 sector-last]; take a prefix but always keep the LAST
    (sector) tag so truncating a long list still lands a working fallback
    tag instead of cutting it off. Falls back to _relevant_hashtags() when
    the field is absent/empty (older cards from before the KG's rollout)."""
    tags = card.get("hashtags") if isinstance(card, dict) else None
    if tags:
        kg_n = KG_HASHTAG_TARGET  # richer target for the KG's own tags only
        return list(tags[:kg_n - 1]) + list(tags[-1:]) if len(tags) > kg_n else list(tags)
    return _relevant_hashtags(sector, entities, n)


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
    # Findings key their date as `timestamp`. Toned-down hook (simple entry link).
    rewritten, hook = _temporal_frame(rewritten, finding.get("timestamp"), kind="finding")
    body = _append_hashtags(rewritten, tags, max_chars)
    return f"{body}\n\n{hook}"


def primary_entities(card: dict, n: int = 3) -> list[dict]:
    """Rank a card's entities by their weight in its relationships
    (confidence * impact), the actor/subject side weighted higher — so hashtags
    and @mentions feature the actual SUBJECT of the event, not whichever entities
    happen to be listed first. Returns entity dicts, highest-weight first.
    Falls back to declared order when a card has no relationships."""
    ents = [e for e in (card.get("entities") or [])
            if isinstance(e, dict) and e.get("name")]
    if not ents:
        return []
    rels = card.get("relationships") or []
    if not rels:
        return ents[:n]
    score = {e["name"]: 0.0 for e in ents}
    for r in rels:
        imp = r.get("impact_magnitude")
        if imp is None:
            imp = r.get("impact")
        w = float(r.get("confidence") or 0) * float(imp or 0)
        if r.get("from") in score:
            score[r["from"]] += w * 1.25   # actor side weighted higher
        if r.get("to") in score:
            score[r["to"]] += w
    return sorted(ents, key=lambda e: score.get(e["name"], 0.0), reverse=True)[:n]


def compose_catalyst(tier: Tier, catalyst: dict, related: list[dict], *, max_chars: int = 280) -> str:
    """Branch-tier composition from a KG catalyst card.

    DETERMINISTIC, no LLM: the KG already authors the post copy. We use the card's
    own share text (or headline) verbatim — same text for every channel — and
    append deterministic hashtags. No arbor-voice rewrite (it fabricated facts),
    no per-channel text divergence. The deep link is appended by the recipe; the
    per-card image is attached per the channel imagery policy."""
    share = catalyst.get("share") or {}
    headline = (catalyst.get("headline") or catalyst.get("title")
                or catalyst.get("subtitle") or "<catalyst>")
    # POST_TEXT_SOURCE: 'share' (default, the KG's authored copy) | 'headline'.
    if str(tier.raw.get("POST_TEXT_SOURCE", "share")).lower() == "headline":
        text = headline
    else:
        text = share.get("linkedin_text") or share.get("twitter_text") or headline
    text = re.sub(r"\s*https?://\S+\s*$", "", text).strip()  # drop trailing source link

    # Hashtags are computed FIRST and their room is reserved before the
    # mechanism clause is sized — otherwise a long mechanism sentence silently
    # starves every hashtag candidate out of the budget with no fallback
    # (_append_hashtags just skips a tag that doesn't fit, no warning). That
    # regression shipped with the mechanism clause itself: most cards with a
    # 100+ char mechanism lost ALL their hashtags, headline-only cards kept
    # theirs — nobody noticed because the text still looked like a normal
    # post, just without tags. Hashtags matter for reach; the mechanism
    # clause is the one that yields room when both can't fit.
    tags = _card_hashtags(catalyst, _sector_for(tier, catalyst), primary_entities(catalyst))
    tag_room = sum(len(t) + 1 for t in tags)  # +1 for the joining space
    # _temporal_frame() may still prepend "Back in <when>: " (~15-25 chars)
    # AFTER this point for an older card — reserve a safety margin for it so
    # that prefix can't itself push a tight fit over budget and re-trigger
    # the same silent drop one layer up.
    _TEMPORAL_PREFIX_ROOM = 25

    # "Why it matters" — one verbatim sentence from the strongest relationship's
    # own `mechanism` field (docs/social-posting-strategy.md Part 2 §1: the
    # headline alone carries no reasoning; this is the field already sitting
    # unused in the payload). Off by a config flag for a clean rollback if it
    # ever reads oddly at scale — default ON, matches "the data already
    # supports this" being the whole point.
    if str(tier.raw.get("MECHANISM_CLAUSE_ENABLED", "true")).strip().lower() != "false":
        mech_room = max_chars - len(text) - 2 - tag_room - _TEMPORAL_PREFIX_ROOM
        if mech_room > 20:  # not worth a fragment shorter than this
            mech = _mechanism_clause(catalyst, max_chars=min(220, mech_room))
            if mech:
                text = f"{text}\n\n{mech}"

    text, hook = _temporal_frame(
        text, catalyst.get("date"), card=catalyst,
        source_id=str(catalyst.get("card_id") or catalyst.get("id") or ""))
    body = _append_hashtags(text, tags, max_chars)
    return f"{body}\n\n{hook}"


_SENTIMENT_WORD = {"+": "tailwind", "-": "headwind", "?": "signal to watch"}


def compose_graph(tier: Tier, catalyst: dict, related: list[dict], *, max_chars: int = 500) -> str:
    """Standalone catalyst-GRAPH post — the entity dependency map, as its own
    post on its own schedule (docs/graph-posters.md), not a second image
    bundled into compose_catalyst's post.

    DETERMINISTIC, no LLM — same reasoning as compose_catalyst: an LLM asked
    to narrate "future outlook" from a card would be asked to speculate past
    what the data supports, which is exactly the fabrication risk
    compose_catalyst was written to avoid. Instead this reuses
    card_to_graph_spec — the IDENTICAL structured read of `relationships` that
    draws the attached graph image — so the text and the picture never
    disagree, and every claim traces to a `rel` in the card's own data."""
    sector = _sector_for(tier, catalyst)
    spec = card_to_graph_spec(catalyst, sector=sector)
    if not spec or not (spec.direct or spec.indirect):
        # Card has a rendered graph PNG but not enough structure to narrate
        # (shouldn't happen if the caller gated on has_graph(), but never
        # assume) — fall back to the card's own text so the post isn't empty.
        return compose_catalyst(tier, catalyst, related, max_chars=max_chars)

    lines = [f"The dependency map behind {spec.primary}"
             f"{f' — {spec.event}' if spec.event else ''}:"]
    for node in spec.direct:
        tag = f" ({node.note})" if node.note else ""
        lines.append(f"→ {node.name}{tag} — {_SENTIMENT_WORD.get(node.sentiment, 'signal to watch')}")
    for node in spec.indirect:
        lines.append(f"↳ {node.name} — second-order")

    scored = spec.direct or spec.indirect
    sentiments = [n.sentiment for n in scored]
    pos, neg = sentiments.count("+"), sentiments.count("-")
    if pos > neg:
        outlook = "Net tailwind — more of the map benefits than not."
    elif neg > pos:
        outlook = "Net headwind — pressure outweighs the upside here."
    else:
        outlook = "Mixed signal — impact splits across the map."
    lines.append(f"\n{outlook}")
    draft = "\n".join(lines)

    tags = _card_hashtags(catalyst, sector, primary_entities(catalyst))
    text, hook = _temporal_frame(
        draft, catalyst.get("date"), card=catalyst,
        source_id=str(catalyst.get("card_id") or catalyst.get("id") or ""))
    body = _append_hashtags(text, tags, max_chars)
    return f"{body}\n\n{hook}"


def compose_graph_stats(tier: Tier, stats: dict, *, sector: str = "",
                          max_chars: int = 280) -> str:
    """A recurring 'state of the sector' post from catalyst-knowledge-graph's
    own computed `stats{}` (src/export.py::_compute_stats — real, live,
    already in every export, used by no post anywhere before this).
    DETERMINISTIC, no LLM — same contract every composer here holds.

    Honest framing, not a dressed-up trend claim (docs/social-posting-
    strategy.md Part 2 takeaway #3): `top_chokepoint_entity` is the
    most-connected entity ALL-TIME (a raw count, no decay), and
    `fastest_accelerating_relationship` is the most common relationship TYPE
    in the last 14 days (a raw count, no baseline comparison) — the wording
    below says exactly that, not "growth" or "acceleration" math these two
    fields don't compute. catalyst-knowledge-graph's graph_insights[]
    (src/detect.py §2.9a, shipped 2026-09-20) is the real, rate-normalised
    version of this — see compose_graph_insight() below, which
    recipe_graph_stats() now prefers whenever it's available; this function
    remains the fallback for tiers/exports without it.

    Raises ValueError when `stats` has neither field — the caller
    (recipe_sector_digest) treats that as "nothing to post", not an error to
    surface to a reader."""
    choke = stats.get("top_chokepoint_entity")
    fastest = stats.get("fastest_accelerating_relationship")
    if not choke and not fastest:
        raise ValueError("compose_graph_stats: stats has neither top_chokepoint_entity nor fastest_accelerating_relationship")

    sec_label = f" {sector}" if sector else ""
    lines = [f"State of the{sec_label} graph this week:"]
    if choke:
        lines.append(f"→ Most-connected name right now: {choke}.")
    if fastest:
        lines.append(f"→ Busiest relationship type (last 2 weeks): {fastest.replace('_', ' ')}.")
    recent, total = stats.get("catalysts_last_7d"), stats.get("total_catalysts")
    if recent is not None and total is not None:
        lines.append(f"\n{recent} new catalysts this week, {total} tracked overall.")
    draft = "\n".join(lines)

    tags = _relevant_hashtags(sector, [])
    # kind="finding": this has no single card/relationship graph to visualize
    # (it's a sector-wide summary), so the generic "elsewhere in the space"
    # hook applies, not compose_graph's per-card one. date=None -> _card_age_
    # days returns None -> _temporal_frame treats it as "recent" -> no
    # "Back in <when>:" prefix, correctly, since this is a live-now summary.
    text, hook = _temporal_frame(draft, None, kind="finding")
    body = _append_hashtags(text, tags, max_chars)
    return f"{body}\n\n{hook}"


def compose_graph_insight(tier: Tier, insight: dict, *, sector: str = "",
                            max_chars: int = 280) -> str:
    """The sharper 'state of the sector' post compose_graph_stats() docstring
    anticipated: catalyst-knowledge-graph's graph_insights[] (src/detect.py
    §2.9a), a comparative, rate-normalised claim like "NVIDIA: 12 partnerships
    in 30 days (3.2x prior quarter)" instead of an all-time raw count.
    DETERMINISTIC, no LLM — same contract every composer here holds.

    `insight["headline"]` is already publish-ready per detect.py's own
    contract ("a headline here IS published copy — it must be literally true
    and specific") — quoted verbatim, never re-derived from the raw numbers.

    Raises ValueError when `insight` has no headline — the caller
    (recipe_graph_stats) treats that as "nothing to post", not an error to
    surface to a reader."""
    headline = insight.get("headline")
    if not headline:
        raise ValueError("compose_graph_insight: insight has no headline")

    sec_label = f" {sector}" if sector else ""
    draft = f"Sector signal{sec_label}:\n{headline}"

    tags = _relevant_hashtags(sector, [])
    # Same framing rationale as compose_graph_stats: no single card to
    # visualize, and this is a live-now claim, not a "Back in <when>" one.
    text, hook = _temporal_frame(draft, None, kind="finding")
    body = _append_hashtags(text, tags, max_chars)
    return f"{body}\n\n{hook}"
