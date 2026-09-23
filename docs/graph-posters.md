# Catalyst graph posters — wiring guide for soljet-postiz

**Producer:** catalyst-knowledge-graph (robotics-render). **Consumer:** this repo.
**Status:** images are LIVE in GCS for every catalyst; nothing in this repo
consumes them yet. Written 2026-09-16.

## What they are

A second poster per catalyst: the entity **subgraph** for that card — the same
picture a visitor sees when they open `robotics.arboryx.ai/card/<id>`. Same
Cytoscape code, colors and layout as the site (node fill = heat, node size =
connection count, edge color = evidence type, dashed = speculative/invalidated),
rendered headless by robotics-render and screenshotted at poster size.

It is NOT the card poster reshaped — it is different information. The card sells
the headline; the graph shows who is connected to whom.

## Where to get them

| | Cards (today) | Graphs (new) |
|---|---|---|
| GCS | `gs://robotics-cards/cards/<card_id>.png` | `gs://robotics-cards/graphs/<card_id>.png` |
| Public URL | `https://robotics.arboryx.ai/card-img/<card_id>.png` | `https://robotics.arboryx.ai/graph-img/<card_id>.png` |
| Size | 2400x1260 | 2400x1260 (same 1.9:1) |

Same bucket, same service account you already use for `RENDER_GCS_BUCKET` —
only the prefix differs. No new credentials.

## Freshness guarantee

Graphs are rendered in the **same** `/render-batch` pass that renders cards
(Pub/Sub fan-out after each daily ingest), with the same skip-if-exists rule.
So: every card PNG that exists has a graph PNG beside it, and new catalysts get
both within the same run. All pre-existing catalysts were backfilled.

Treat a missing graph as "not ready", never as an error — fail closed and post
exactly as you do today.

## Suggested config (mirrors the card keys)

```ini
# tier.config — products/arboryx.ai/branches/robotics/tier.config
RENDER_GCS_GRAPH_PREFIX="graphs"          # sibling of RENDER_GCS_PREFIX
KG_GRAPH_URL_TEMPLATE="https://robotics.arboryx.ai/graph-img/{card_id}.png"

# Per-channel: none | second_image | alternate
GRAPH_IMAGE_POLICY_LINKEDIN="second_image"
GRAPH_IMAGE_POLICY_X="none"
```

`src/lib/card_images.py` already has everything needed — `_gcs_list_ids()` and
`has_render()` just need the prefix parameterised, so a `has_graph(tier, card_id)`
is a few lines rather than a new module.

## Recommended per-channel usage

- **LinkedIn — attach both** (card first, graph second). LinkedIn renders
  multi-image posts as a swipeable pair; the card wins the scroll, the graph
  earns the dwell time. This is the highest-value use.
- **X — leave as is initially** (`none`). You already attach the card natively
  and put the deep link in a self-reply; a second image competes with that
  reply for attention. If you want to test it, the cleanest slot is attaching
  the graph to the `X_REPLY_LINK` reply, not the root post.
- **Never post the graph alone.** Out of context it reads as an abstract
  diagram — it needs the card's headline next to it.

## Per-card hashtags (`card.hashtags`)

Every card (cards.json + `catalysts/items/{entry_id}`) now carries
`hashtags: [...]`, computed deterministically at ingest and stored per
catalyst. Priority-ordered — entity, topic, theme, sector last — so take a
prefix to fit the channel budget and keep the sector tag:

```python
tags = card.get("hashtags") or []
picked = tags[:HASHTAG_TARGET - 1] + tags[-1:] if len(tags) > HASHTAG_TARGET else tags
```

This replaces `composer._relevant_hashtags` / `SECTOR_HASHTAGS` /
`_camel_tag`; keep them only as a fallback when `hashtags` is empty
(older docs before the first post-deploy ingest).

## Gotchas

1. The `/graph-img/**` route is served by the `robotics-og` Cloud Run service
   via a Firebase Hosting rewrite. If the URL 404s, the hosting deploy that
   adds that rewrite has not run yet — use the GCS path in the meantime.
2. Graphs are sparse for catalysts with one or two entities; that is correct,
   not a render failure. If you want a floor, skip attaching when the card has
   fewer than 3 entities.
3. Both images live in a **private** bucket. The public route streams them; do
   not hand out signed GCS URLs.

---

# `graph_insights[]` — now populated (2026-09-20)

The field technical_spec §2.9a asked for is live in the KG repo
(`src/detect.py`). It is **not** in the per-card documents you already read —
it is one array per sector, so it lives on the graph doc:

```
Firestore: CKG-Robotics/graph/sectors/Robotics  →  field: graph_insights
```

One extra document read per run. It is deliberately not mirrored into
`catalysts/items/*` — the same array copied onto 640+ card docs would bloat
every doc and every write for no new information.

## Shape

```jsonc
[
  {"type": "chokepoint", "entity": "XPeng", "growth_ratio": 25.29,
   "recent_count": 59, "baseline_count": 7,
   "window_days": 30, "baseline_days": 90,
   "headline": "XPeng: 59 competitive collisions in 30 days (25.3x prior 90-day rate)"},

  {"type": "velocity", "rel_type": "pilots", "growth_ratio": 3.9,
   "recent_count": 78, "baseline_count": 60,
   "window_days": 30, "baseline_days": 90,
   "headline": "Pilot deployments across the sector: 78 in 30 days (3.9x prior 90-day rate)"}
]
```

- `type: "chokepoint"` → the subject is an **entity** (`entity` key present).
- `type: "velocity"` → the subject is a **relationship type** across the whole
  sector (`rel_type` key present).
- Sorted strongest-claim-first. Empty array = nothing cleared the thresholds
  that day; that is a normal outcome, not an error — skip the post.

## Rules of use

1. **Quote `headline` verbatim.** Same contract as every other composer on
   your side: never re-derive a claim from `recent_count`/`growth_ratio`
   yourself. The counts are exposed for filtering, not for prose.
2. **`growth_ratio` can be `null`.** That means the entity had too thin a
   prior baseline to support a multiplier (fewer than 3 prior edges). The
   `headline` is still safe to publish — it states raw counts instead
   ("50 in 30 days (vs 1 in the prior 90)"). If you only want multiplier
   claims, filter on `growth_ratio is not None`.
3. **These are sector-level, not card-level.** They suit the "state of the
   sector" slot in `docs/social-posting-strategy.md` Part 2 §3 — the post
   that currently has to fall back to honest-but-dull counts. One insight
   per post; do not staple them onto a catalyst card post.
4. **Freshness:** recomputed on every KG export (each daily ingest), over a
   rolling 30-day window vs the prior 90. A given claim will persist for
   several days — dedupe on `headline` against what you have already posted.

## Why the thresholds exist

Live data on 2026-09-20 produced `"JD.com: 50 supply agreements in 30 days
(150.0x prior 90-day rate)"` — arithmetically true, off a baseline of exactly
one edge. That headline would have been published verbatim. The KG side now
refuses a multiplier below 3 baseline edges (`insights.min_baseline`), plus
floors on recent volume (`min_recent`) and ratio (`min_growth_ratio`). If a
claim ever looks inflated, those knobs are in the KG's `config/config.yaml`,
not here.
