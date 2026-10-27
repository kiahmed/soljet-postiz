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

> **Wording changed 2026-10-26** (see "Weekly recap" below): an entity whose
> edges are of mixed types now reads e.g. `"Booster Robotics: 16 new
> connections in 30 days (9.6x prior 90-day rate), led by 8 competitive
> collisions"` (live, 2026-10-26). The old form labelled all 16 as the top
> type — overstated; the XPeng example above has the same flaw. Still
> quote `headline` verbatim; new fields `top_rel` / `top_rel_count` / `hashtags`.

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

---

# Weekly recap — digest record, images, hashtags (2026-10-26)

**Status:** committed in the KG repo (`10afe7a`), **live once the KG deploy
lands**. Up to 6 cards per week: up to 5 `entity` cards + 1 `sector` card
(generic category names are filtered out of the sector card; it's omitted
only when there's no trend insight or every name is generic). Until then the fields below are absent — keep posting the digest
text-only. Detect readiness by presence, never by date.
KG spec: `technical_spec.md` §2.9b.

## What changed on the KG side

1. **Honest entity headlines.** `graph_insights[]` chokepoint headlines no
   longer call all of an entity's edges by its top type:
   `"HD Hyundai: 16 new connections in 30 days (9.6x prior 90-day rate), led by 9 partnerships"`.
   Single-type entities read as before. New fields: `top_rel`, `top_rel_count`.
2. **Hashtags on every insight row** — `graph_insights[i].hashtags`
   (entity, its topic, sector; sector trends: topic + sector). Stop guessing.
3. **A `recap` block** for the week: chart cards with a one-line verdict, and
   a rendered image per card.

## Where things are

| What | Location | Lifetime |
|---|---|---|
| Insights (+ `hashtags`) | `CKG-Robotics/graph/sectors/Robotics` → `graph_insights` | overwritten daily |
| Recap, live copy | same doc → `recap` | overwritten daily |
| **Recap, the week's record** | `CKG-Robotics/recaps/weeks/<YYYY-Www>` (e.g. `2026-W44`) | **write-once per schema** — created by the first export of the ISO week that has recap cards (normally Monday's; can slip a day if Monday has none). Rewritten exactly once if the KG ships a newer `schema` (never downgraded); key caches on `(week, schema)` |
| Recap card images | `gs://robotics-cards/recaps/Robotics/<YYYY-Www>/<card.id>.png` | **write-once** (only a manual `recaps_force` re-render overwrites); deleted by the bucket's 90-day lifecycle rule |

Images are in the same **private** bucket as card PNGs, read the same way
(`storage.Client()` with your runtime's Application Default Credentials; no
public route yet). Bucket IAM is bucket-wide, not per-prefix, so whatever
lets `card_images.py` read `cards/` also reads `recaps/`. **Verify in the
runtime that posts** — the only evidence so far is this repo's own local
cache (`data/render_gcs_cache.json`, gitignored) showing a successful
`cards/` listing on 2026-09-05 from a local run, which may not be the
posting identity:

```bash
gcloud storage ls gs://robotics-cards/recaps/Robotics/   # as the posting identity
```

`_gcs_list_ids` swallows a 403; for recap images log a missing/denied read
loudly, or a permissions gap silently turns every digest text-only.

## Record shape (`recap`, identical in both places) — schema 2 (2026-10-27)

Illustrative (2026-W44 values; the week doc additionally carries
`"sector": "Robotics"`). **Every card has a `kind`** — that's how you tell
what you're attaching:

```jsonc
{
  "schema": 2,                       // 1 = old separate mix/grounding/concentration cards
  "week": "2026-W44", "as_of": "2026-10-27", "window_days": 30, "baseline_days": 90,
  "groups": ["Competition", "Partnership", "Deployment", "Supply & tech", "Capital & talent", "Other"],
  "hashtags": ["#SAP", "#HuggingFace", "#Integration", "#Robotics"],   // digest default
  "cards": [                         // ordered: lead company, the sector card, other companies
    {"id": "entity-sap", "kind": "entity", "entity": "SAP",
     "insight_headline": "SAP: 25 new connections in 30 days (12.5x …), led by 10 competitive collisions",
     "title":   "What's behind SAP's surge?",
     "verdict": "SAP may be becoming a hub others join.",   // ≤10 words — the takeaway
     "hedged":  true,                // verdict softened: <40% of its links reported/cited
     "big": "25 new connections", "sub": "in the last 30 days · was about 2 a month before",
     "sentence": "Mostly partnerships: 15 of 25.",
     "confirmation": {"big": "36% confirmed", "verdict": "Early signal: mostly inference, not yet reported.",
                      "sentence": "9 are reported or cited by sources; 16 are our model's inference."},
     "hashtags": ["#SAP", "#Partnership", "#Robotics"],
     "data": {"mix": {…}, "grounding": {…}}},              // chart data — don't parse
    {"id": "sector-integrates_with", "kind": "sector", "rel_type": "integrates_with", …},
    {"id": "entity-hugging-face", "kind": "entity", "entity": "Hugging Face", …}
    // … one "entity" card per chokepoint insight, up to 5
  ]
}
```

Card kinds:
- **`entity`** — ONE image per company: what its surge is made of (verdict +
  before/now chart) **and** how much of it is confirmed (a sub-panel on the
  same image). There is no separate "confirmed" card any more.
- **`sector`** — who the sector leans on, now vs before; matches the velocity
  insight via `rel_type`.

Coverage: one `entity` card for each of the top **5** chokepoint insights,
so the companies your digest picks (after your 21-day skip) have a card.
2026-10-27 a digest named HD Hyundai (insight #3) when cards only covered the
top 2 — that's what this fixes.

## How to build the Monday digest post (do it in this order)

1. **Read the week record**, not the live copy:
   `week = now_utc.strftime("%G-W%V")` → `CKG-Robotics/recaps/weeks/{week}`.
   Missing → fall back to today's text-only digest (graph doc
   `graph_insights`), exactly as now.
2. **Pick insights as you do today** from `graph_insights` (strongest first,
   your 21-day `posted_log` skip). The recap does NOT apply your skip — you do.
3. **Text:** quote each picked row's `headline` verbatim (unchanged rule).
4. **Lead image + opening line:** for the first picked row, find its card —
   chokepoint row → card with `kind == "entity"` and `entity == row.entity`;
   velocity row → card with `kind == "sector"` and
   `rel_type == row.rel_type`. (Archived schema-1 weeks used kinds
   `mix`/`concentration`; treat `mix` like `entity`.) Open the post with that card's **`verdict`**
   (it is the takeaway; the headline is the evidence) and attach
   `recaps/Robotics/{week}/{card.id}.png`.
   No matching card (your post's subjects aren't among the up-to-5
   `entity` cards, e.g. your skip went past insight #5) → use `cards[0]`'s
   image only if its subject is in your post; otherwise post text-only. **Never attach an image for a subject the text doesn't mention.**
5. **Hashtags:** union the `hashtags` of exactly the rows you posted, in row
   order, deduped, then take a prefix to fit the channel (keep the sector tag
   last, same helper as `card.hashtags`). Use `recap.hashtags` only if you post
   the recap's default set unchanged. Never invent tags.
6. **Image not there yet** (render runs after ingest; Monday's can lag the
   export by minutes): check existence, retry later in the window, else post
   text-only. A missing image is "not ready", never an error.
7. **Link:** keep the site root for now; the Recap tab is
   `https://robotics.arboryx.ai/#view=recap` (sign-in required; premium-gated
   later).

## Rules

- **Don't re-derive numbers or verdicts.** `verdict`, `big`, `sentence` are
  computed from the same windows as the headlines; rewording them can make
  the image and text disagree.
- **Each `entity` image already carries its own "how much is confirmed"
  panel** — never source that number from anywhere else. If `hedged` is true,
  keep the verdict's "may be" wording; don't firm it up.
- **Cache on the image URL and on the week record keyed by `(week,
  schema)`** — images are write-once (only a manual `recaps_force` re-render
  overwrites one; objects expire after 90 days); the record is write-once per
  schema (a newer `schema` rewrites it once, with new card ids → new images). Unlike `graphs/<card_id>.png`, which
  routine `force` backfills re-render in place.
- Footer on every image already says *Information only — not investment
  advice*; keep posts free of buy/valuation language to match.
