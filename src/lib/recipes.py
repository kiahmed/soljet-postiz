"""Recipe layer: ways to compose a post bundle from different inputs.

Each recipe returns a PostBundle that the dispatcher can push to Postiz.
Adding a new recipe = one new function here + one CLI sub-arg.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .composer import (_llm_rewrite, _read_context, compose_catalyst, compose_finding,
                       compose_graph, compose_graph_insight, compose_graph_stats)
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

        if ds.type == "matrix_api":
            return _matrix_bundle(tier, item)

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


# ---------- Catalyst graph — standalone post, own schedule (docs/graph-posters.md) ----------

# Richer than the 280-char card-post budget — the dependency-map narrative
# needs room for several entities; split_for_thread turns any overflow into
# an X thread automatically, same as any other over-length post.
GRAPH_POST_CHAR_BUDGET = 500


def recipe_graph(tier: Tier, source_id: str) -> PostBundle:
    """One catalyst-graph post: the entity dependency map for a card, as its
    OWN post — not a second image bundled into recipe_single's card post.
    Caller (bin/daily.py --kind graph) gates on card_images.has_graph()
    before calling this, same fail-closed contract as the card recipe."""
    for ds in tier.sources:
        if ds.type not in ("cards_json", "firestore_cards"):
            continue
        try:
            src = build_source(ds, tier)
            item = src.get(source_id)
        except (KeyError, ValueError):
            continue
        related = src.get_related(source_id)
        link = deep_link_for(tier, "cards_json", item) or ""
        budget = _budget_for_link(link, total=GRAPH_POST_CHAR_BUDGET)
        ctx = {
            "source_url": "",
            "sector": tier.raw.get("SECTORS", "").split(",")[0].strip()
                      or item.get("sector") or "",
            "title": (item.get("headline") or "")[:120],
            "subtitle": item.get("subtitle") or "",
            "card": item,  # full card dict — imagery layer resolves the graph PNG from it
        }
        if link:
            ctx["deep_link"] = link
        text = compose_graph(tier, item, related, max_chars=budget)
        text = append_link_to_text(text, link)
        return PostBundle(
            text=text,
            source_type=ds.type,   # "cards_json" or "firestore_cards"
            source_id=source_id,
            context=ctx,
        )
    raise KeyError(f"source-id '{source_id}' not found in tier '{tier.id}' "
                   f"(no cards_json/firestore_cards source configured)")


def recipe_graph_stats(tier: Tier) -> PostBundle:
    """A recurring 'state of the [sector] graph' post (docs/social-posting-
    strategy.md Part 2 §3/Phase-1-item-3) from stats{} already computed by
    catalyst-knowledge-graph's export — not tied to any one card, so there's
    no per-card source_id; a per-ISO-week one is synthesized instead so a
    second call the same week is at least recognizable as a repeat by
    anything checking posted_log, even though this recipe doesn't itself
    gate on it (manually invoked for now, see bin/graph_stats_post.py).

    NOT the same thing as recipe_sector_digest() below (an older, unrelated
    recipe: top-N Firestore findings in a sector, LLM-summarized) — picked
    a distinct name specifically to avoid shadowing that existing function.

    Prefers catalyst-knowledge-graph's graph_insights[] (src/detect.py
    §2.9a, shipped 2026-09-20) when the source exposes a non-empty one —
    a comparative, rate-normalised claim ("3.2x prior quarter") beats the
    coarser all-time stats{} fields, exactly as compose_graph_stats()'s own
    docstring anticipated. Falls back to stats{} when graph_insights[] is
    absent/empty (e.g. SECTOR_STATS_DOC unset, or the sector's data is too
    thin for any detector to fire) so this recipe keeps working unchanged
    for tiers that haven't turned insights on yet.

    Raises KeyError (same "nothing to post" contract as every other recipe
    here) when the tier's source exposes neither a usable graph_insights[]
    nor a usable stats{} — a data-less week just means no post that week,
    never a broken one."""
    for ds in tier.sources:
        if ds.type not in ("cards_json", "firestore_cards"):
            continue
        src = build_source(ds, tier)
        sector = tier.raw.get("SECTORS", "").split(",")[0].strip() or (
            tier.id.split(".")[-1] if tier.parent_id else "")
        week = datetime.now(timezone.utc).strftime("%G-W%V")
        source_id = f"GRAPH-STATS-{tier.id}-{week}"

        get_insights = getattr(src, "graph_insights", None)
        insights = get_insights() if callable(get_insights) else []
        if insights:
            text = compose_graph_insight(tier, insights[0], sector=sector)
            return PostBundle(
                text=text,
                source_type="graph_stats",
                source_id=source_id,
                context={"title": text.splitlines()[0][:120] if text else ""},
            )

        get_stats = getattr(src, "stats", None)
        if not callable(get_stats):
            continue
        stats = get_stats() or {}
        if not stats.get("top_chokepoint_entity") and not stats.get("fastest_accelerating_relationship"):
            continue
        text = compose_graph_stats(tier, stats, sector=sector)
        return PostBundle(
            text=text,
            source_type="graph_stats",
            source_id=source_id,
            context={"title": text.splitlines()[0][:120] if text else ""},
        )
    raise KeyError(f"tier '{tier.id}': no source exposes graph_insights() or stats(), or neither has usable fields")


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

    if card.get("_off_hours_catalyst"):
        bits.append("Alert generated while markets were closed, based on a "
                    "catalyst and the last available options chain — check "
                    "back at the next market open.")

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


def _minimal_simmer_card(source_id: str, symbol: str, state: str | None,
                         expiry: str | None) -> dict:
    """A card built from the Pub/Sub event alone — used when the read-only API
    has no stored readiness for the ticker yet (event fired ahead of the engine
    write, or a synthetic fire). compose_simmer degrades gracefully on the
    missing metrics."""
    sym = (symbol or "").upper()
    st = state or "watch_entered"
    return {
        "card_id": source_id, "id": source_id, "symbol": sym, "state": st,
        "expiry": (str(expiry)[:10] if expiry else ""),
        "metrics": {}, "sentiment": {}, "gates": {},
        "headline": f"${sym} — {'ready to serve' if st == 'ready' else 'started simmering'}",
        "entities": [{"name": sym, "x_handle": f"${sym}" if sym else None,
                      "linkedin_handle": None}],
        "url": f"https://simmer.facades.trade/?symbol={sym}",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "_enrich": "minimal",
    }


def recipe_simmer(tier: Tier, source_id: str, *, state: str | None = None,
                  symbol: str | None = None, expiry: str | None = None,
                  off_hours_catalyst: bool = False) -> PostBundle:
    """Simmer post for one ticker state-change. `state` (from the Pub/Sub event:
    watch_entered | ready | …) overrides whatever the API's current decision
    implies, so an event fired on the transition posts the right moment even if
    the engine has moved on by the time we re-pull. If the read-only API has no
    card for the ticker (KeyError), fall back to a minimal card from the event
    attributes rather than dropping the post — `symbol` must then be given.

    `off_hours_catalyst`: set by bin/simmer_poster.py only when the market is
    actually closed AND the event itself carried the `off_hours_catalyst`
    attribute (EdgeLane's call, not this function's) — makes
    compose_simmer() append the disclaimer line (see docs/simmer_integration.md
    §Market hours)."""
    src = build_source(tier.sources[0], tier)
    try:
        card = src.get(source_id)
    except KeyError:
        if not symbol:
            raise
        card = _minimal_simmer_card(source_id, symbol, state, expiry)
    if state:
        card["state"] = state
    if off_hours_catalyst:
        card["_off_hours_catalyst"] = True
    return _simmer_bundle(tier, card)


# ---------- Matrix (Facades) — event-driven, deterministic templating ----------

# Tag -> plain-English clause. Order matters: more specific combos are checked
# first (HEALTHY+LIQ HIGH beats a bare HEALTHY). Never free text / an LLM
# paraphrase — the copy can't drift from what the tags actually say.
_MATRIX_TAG_CLAUSES: list[tuple[frozenset[str], str]] = [
    (frozenset({"HEALTHY", "LIQ HIGH"}), "liquidity's deep enough to size into"),
    (frozenset({"BROKEN"}), "the model's edge assumption didn't hold up"),
    (frozenset({"MARGINAL"}), "on the edge — thin liquidity or a slim edge, worth a "
                              "second look before sizing up"),
    (frozenset({"TRADEABLE ON LIMIT"}), "workable, but only at a limit price, not the market"),
    (frozenset({"DO NOT TRADE"}), "the engine is flagging this one to sit out"),
    (frozenset({"HEALTHY"}), "the setup checks out clean"),
]


def _matrix_tag_clause(tags: list[str]) -> str | None:
    have = {str(t).upper() for t in (tags or [])}
    for wanted, clause in _MATRIX_TAG_CLAUSES:
        if wanted <= have:
            return clause
    return None


def _matrix_grid_tags(entry: dict) -> list[str]:
    """Same HEALTHY/LIQ HIGH/TRADEABLE-ON-LIMIT reconstruction _to_card() does
    for the primary pick, applied to one entry of the `grid` block (each of
    the 8 candidate structures carries the same health/liquidity/verdict
    shape)."""
    tags = []
    if entry.get("health"):
        tags.append(str(entry["health"]).upper())
    if entry.get("liquidity"):
        tags.append(f"LIQ {str(entry['liquidity']).upper()}")
    verdict = entry.get("composite_verdict") or {}
    if verdict.get("label"):
        tags.append(str(verdict["label"]).upper())
    return tags


def compose_matrix(tier: Tier, card: dict, *, max_chars: int = 260) -> str:
    """Deterministic post text from Matrix engine state. No LLM — same rule as
    compose_simmer(): the numbers and tags ARE the message.

    Seven states across 6 moments (docs/matrix_integration.md §Post moments —
    bias_aligned/bias_diverged are the two outcomes of one moment):
    `pick_selected`, `bias_aligned`, `bias_diverged`, `win_rate_notable`,
    `session_open`, `grid_digest`, `daily_recap`. All draw on whatever blocks
    MatrixAPI.get() fetched (pick/bias/grid/win_eval/walls) — never on data
    the card doesn't carry."""
    sym = (card.get("symbol") or "").upper()
    st = card.get("state") or "pick_selected"
    strategy = card.get("strategy") or "a setup"
    composite = _fmt_num(card.get("composite"), nd=1)
    clause = _matrix_tag_clause(card.get("tags") or [])
    hint = (card.get("hint_text") or "").strip()

    bits: list[str]
    if st == "daily_recap":
        bits = [f"Best setup today on ${sym}: {strategy}"
                + (f" (composite {composite})" if composite else "") + "."]
        if clause:
            bits.append(clause.capitalize() + ".")
        grid = card.get("grid") or {}
        if grid:
            worst_key = min(grid, key=lambda k: grid[k].get("composite_score", 999) or 999)
            worst = grid[worst_key] or {}
            w_label = worst.get("label") or worst_key.replace("_", " ").title()
            w_score = _fmt_num(worst.get("composite_score"), nd=1)
            w_clause = _matrix_tag_clause(_matrix_grid_tags(worst))
            line = f"Weakest: {w_label}" + (f" (composite {w_score})" if w_score else "") + "."
            if w_clause:
                line += f" {w_clause.capitalize()}."
            bits.append(line)
    elif st == "bias_aligned":
        bits = [f"${sym} — the bias read now agrees with the engine's pick ({strategy})."]
    elif st == "bias_diverged":
        bits = [f"${sym} — the bias read is diverging from the engine's pick ({strategy})."]
    elif st == "win_rate_notable":
        wr = card.get("win_rate")
        graded = card.get("graded")
        if wr is not None and graded:
            bits = [f"${sym}'s win-eval grid: {wr:.0f}% win rate over {graded} graded trades on {strategy}."]
        else:
            bits = [f"${sym}'s win-eval grid just turned a corner on {strategy}."]
    elif st == "pick_result":
        # Reports how an announced pick actually closed — losses included on
        # purpose (EdgeLane/docs/matrix_events_update.md: "a feed that only
        # reports its wins is marketing"). No hint_text on this card (no live
        # bias re-fetch for a closed run — see _pick_result_card), so this is
        # the whole message, not a caveat tacked onto one.
        result = (card.get("result") or "").lower()
        verdict_word = {"win": "won", "loss": "lost"}.get(result, result or "closed")
        bits = [f"${sym} — {strategy} {verdict_word}."]
        entry = _fmt_num(card.get("entry_premium"), nd=2)
        exit_ = _fmt_num(card.get("exit_premium"), nd=2)
        if entry is not None and exit_ is not None:
            bits.append(f"${entry} to ${exit_}.")
        held = card.get("held_minutes")
        try:
            held_i = int(float(held))
        except (TypeError, ValueError):
            held_i = None
        if held_i is not None:
            bits.append(f"Held {held_i} min.")
    elif st == "session_open":
        walls = (card.get("walls") or {}).get("key_levels") or {}
        cw, pw = walls.get("call_wall"), walls.get("put_wall")
        if cw is not None or pw is not None:
            bits = [f"${sym} — today's walls: call {_fmt_num(cw, nd=0) or cw}, "
                    f"put {_fmt_num(pw, nd=0) or pw}."]
        else:
            bits = [f"${sym} — today's session open."]
    elif st == "grid_digest":
        grid = card.get("grid") or {}
        n = len(grid)
        tradeable = sum(1 for v in grid.values()
                        if (v.get("composite_verdict") or {}).get("mode") not in (None, "skip", "wait"))
        if n:
            bits = [f"This week's strategy grid for ${sym} — {n} setups scored, {tradeable} tradeable."]
        else:
            bits = [f"This week's strategy grid for ${sym} — a look at all the setups."]
    else:  # pick_selected (default)
        bits = [f"${sym} — engine pick: {strategy}"
                + (f". Composite {composite}" if composite else "") + "."]
        if clause:
            bits.append(clause.capitalize() + ".")
    if hint:
        bits.append(hint)
    bits.append("A snapshot of engine state, not advice.")

    text = " ".join(b for b in bits if b)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _matrix_bundle(tier: Tier, card: dict) -> PostBundle:
    sym = (card.get("symbol") or "").upper()
    link = deep_link_for(tier, "matrix_api", card) or card.get("url") or ""
    text = compose_matrix(tier, card, max_chars=_budget_for_link(link))
    text = append_link_to_text(text, link)
    return PostBundle(
        text=text,
        source_type="matrix_api",
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


def _minimal_matrix_card(source_id: str, symbol: str, state: str | None,
                         expiry: str | None = None) -> dict:
    """A card built from the Pub/Sub event alone — used when the read-only API
    has no stored pick for the ticker (the API doesn't exist yet at all, as of
    this writing — see docs/matrix_integration.md — so this is the path every
    real event takes until EdgeLane ships /matrix/state/<SYM>).
    compose_matrix degrades gracefully on the missing composite/tags."""
    sym = (symbol or "").upper()
    st = state or "pick_selected"
    return {
        "card_id": source_id, "id": source_id, "symbol": sym, "state": st,
        "expiry": (str(expiry)[:10] if expiry else ""),
        "strategy": None, "composite": None, "tags": [], "hint_text": None,
        "headline": f"${sym} — Matrix {st.replace('_', ' ')}",
        "entities": [{"name": sym, "x_handle": f"${sym}" if sym else None,
                      "linkedin_handle": None}],
        "url": f"https://matrix.facades.trade/?symbol={sym}",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "_enrich": "minimal",
    }


def _pick_result_card(source_id: str, symbol: str, expiry: str | None,
                      event_attrs: dict) -> dict:
    """`pick_result` reports on a run that has already CLOSED — there is no
    live API block for it (unlike pick_selected, which re-reads the CURRENT
    pick), so the Pub/Sub event's own attributes are the only source of
    truth. EdgeLane merges the original pick_selected summary
    (strategy/label/composite_score/verdict/structure) with the result fields
    (result/entry_premium/exit_premium/favorable_delta/held_minutes) — see
    EdgeLane/docs/matrix_events_update.md "New state: pick_result"."""
    sym = (symbol or "").upper()
    # EdgeLane's _pick_summary() (matrix_signals.py) only carries the pick's
    # raw `strategy` slug ("bull_put") and `label` — NOT `short`/`name`, the
    # fields matrix_source.py._to_card() prefers for the live pick_selected
    # path. `label` on the top-level pick is a risk-style tag ("Aggressive"),
    # not the strategy name — confirmed 2026-09-23 when a pick_result post
    # read "$NDX — Aggressive won." instead of "$NDX — Bull Put won." So:
    # humanize the slug ourselves rather than trust label as a display name.
    raw_strategy = event_attrs.get("strategy") or ""
    strategy_name = raw_strategy.replace("_", " ").title() if raw_strategy else None
    return {
        "card_id": source_id, "id": source_id, "symbol": sym, "state": "pick_result",
        "expiry": (str(expiry)[:10] if expiry else ""),
        "strategy": strategy_name,
        "composite": event_attrs.get("composite_score"),
        "tags": [], "hint_text": None,
        "result": event_attrs.get("result"),
        "entry_premium": event_attrs.get("entry_premium"),
        "exit_premium": event_attrs.get("exit_premium"),
        "favorable_delta": event_attrs.get("favorable_delta"),
        "held_minutes": event_attrs.get("held_minutes"),
        "headline": f"${sym} — Matrix pick result",
        "entities": [{"name": sym, "x_handle": f"${sym}" if sym else None,
                      "linkedin_handle": None}],
        "url": f"https://matrix.facades.trade/?symbol={sym}",
        "date": datetime.now().strftime("%Y-%m-%d"),
    }


def recipe_matrix(tier: Tier, source_id: str, *, state: str | None = None,
                  symbol: str | None = None, expiry: str | None = None,
                  event_attrs: dict | None = None) -> PostBundle:
    """Matrix post for one symbol's state-change. `state` (from the Pub/Sub
    event) overrides whatever the API's current read implies. If the read-only
    API has no card for the ticker (KeyError — currently ALWAYS, until
    EdgeLane ships the endpoint), fall back to a minimal card from the event
    attributes rather than dropping the post — `symbol` must then be given.

    `pick_result` is a special case: it describes a CLOSED run, so there is
    nothing to re-fetch live — the card is built straight from the event's
    own attributes (`event_attrs`, the full normalized Pub/Sub message)
    instead of calling the source at all."""
    if state == "pick_result":
        if not symbol:
            raise ValueError("pick_result requires symbol")
        return _matrix_bundle(tier, _pick_result_card(source_id, symbol, expiry,
                                                        event_attrs or {}))
    src = build_source(tier.sources[0], tier)
    try:
        card = src.get(source_id)
    except KeyError:
        if not symbol:
            raise
        card = _minimal_matrix_card(source_id, symbol, state, expiry)
    if state:
        card["state"] = state
    return _matrix_bundle(tier, card)


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
