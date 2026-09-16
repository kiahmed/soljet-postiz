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

## Gotchas

1. The `/graph-img/**` route is served by the `robotics-og` Cloud Run service
   via a Firebase Hosting rewrite. If the URL 404s, the hosting deploy that
   adds that rewrite has not run yet — use the GCS path in the meantime.
2. Graphs are sparse for catalysts with one or two entities; that is correct,
   not a render failure. If you want a floor, skip attaching when the card has
   fewer than 3 entities.
3. Both images live in a **private** bucket. The public route streams them; do
   not hand out signed GCS URLs.
