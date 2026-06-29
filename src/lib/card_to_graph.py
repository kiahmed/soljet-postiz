"""Map a curated KG card (cards.json shape) → GraphSpec.

This is the *cheap* path for tier-2 catalyst posts — no LLM call needed,
since the card already carries `entities` + `relationships` in structured
form. Falls through to imagery_graph.extract_graph (LLM) only when the
card lookup fails or yields no usable data.

Card shape (per catalyst-knowledge-graph/frontend/cards.json):
  card_id        e.g. "ROB-041726-001"
  card_type      "partnership" | "raise" | "milestone" | ...
  date           "YYYY-MM-DD"
  headline       short headline
  subtitle       1-2 sentences of context
  entities       [{name, ticker, type}, ...]
  relationships  [{from, rel, to, confidence, evidence_type, mechanism,
                   mechanism_strength, impact_magnitude, flagged, source_refs}, ...]
"""
from __future__ import annotations

from .imagery_graph import GraphNode, GraphSpec


# Relationship verbs that are clearly positive/negative for the FROM entity.
# Anything else routes to '?' (ambiguous) — better than guessing wrong.
_POS_RELS = {
    "partners_with", "invests_in", "acquires", "supplies_to", "wins_contract_with",
    "raises_from", "secures_funding_from", "contracts_with", "deploys_at",
    "endorsed_by", "selected_by", "anchored_by",
}
_NEG_RELS = {
    "competes_with", "displaces", "litigates_with", "sues", "loses_contract_to",
    "underbid_by", "regulated_by", "investigated_by",
}


def _sentiment_for(rel: str, impact: float | None) -> str:
    rel = (rel or "").lower()
    if rel in _POS_RELS:
        return "+"
    if rel in _NEG_RELS:
        return "-"
    # Magnitude alone doesn't tell us direction — only verb does.
    return "?"


def _short_note(rel: str, mechanism: str, impact: float | None) -> str:
    """Short human-readable tag for the node. Prefers the verb, then a
    quantitative impact tag, then a clipped mechanism phrase."""
    rel_human = (rel or "").replace("_", " ").strip()
    if rel_human and len(rel_human) <= 14:
        return rel_human
    if isinstance(impact, (int, float)) and impact:
        return f"impact {impact:.2f}"
    if mechanism:
        return mechanism.split(".")[0][:14].strip()
    return ""


def card_to_graph_spec(card: dict, sector: str = "") -> GraphSpec | None:
    """Returns a GraphSpec or None if the card lacks enough structure."""
    entities = card.get("entities") or []
    rels = card.get("relationships") or []
    if not entities:
        return None

    headline = (card.get("headline") or "").strip()
    subtitle = (card.get("subtitle") or "").strip()
    primary_name = entities[0]["name"]
    primary_label = primary_name[:36]

    # Build a short event tag — card_type + date is informative, falls back to
    # the headline's first noun phrase if either is missing.
    bits = []
    ct = (card.get("card_type") or "").replace("_", " ").strip()
    if ct:
        bits.append(ct)
    if card.get("date"):
        bits.append(card["date"])
    event = " · ".join(bits)[:30] or headline[:30]

    # Direct: relationships with the primary entity on the FROM side.
    direct: list[GraphNode] = []
    seen: set[str] = set()
    for r in rels:
        if (r.get("from") or "").lower() != primary_name.lower():
            continue
        target = r.get("to") or ""
        if not target or target.lower() in seen:
            continue
        seen.add(target.lower())
        direct.append(GraphNode(
            name=target[:30],
            sentiment=_sentiment_for(r.get("rel", ""), r.get("impact_magnitude")),
            note=_short_note(r.get("rel", ""), r.get("mechanism", ""), r.get("impact_magnitude")),
        ))
        if len(direct) >= 4:
            break

    # Indirect: any other entities mentioned in the card that aren't the primary
    # and aren't already a direct target. Sentiment is unknown for these.
    indirect: list[GraphNode] = []
    direct_names = {d.name.lower() for d in direct}
    for e in entities[1:]:
        nm = (e.get("name") or "").strip()
        if not nm or nm.lower() == primary_name.lower() or nm.lower() in direct_names:
            continue
        indirect.append(GraphNode(name=nm[:30], sentiment="?", note=(e.get("ticker") or e.get("type") or "")[:14]))
        if len(indirect) >= 3:
            break

    if not direct and not indirect:
        return None

    return GraphSpec(
        primary=primary_label,
        event=event,
        sector=sector or "",
        direct=direct,
        indirect=indirect,
    )
