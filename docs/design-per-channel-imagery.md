# Design: per-channel imagery policy (X link-card / LinkedIn native image)

**Status: PROPOSED — not implemented.** Drafted 2026-07-05 from the gap
analysis between this repo and catalyst-knowledge-graph's
`tools/robotics-social` container (see that repo's `docs/workbench.md`,
entries 2026-07-05*).

---

## Why

LinkedIn demoted **organic link posts** to a compact thumbnail in 2024 —
og:image size/quality no longer matters; the feed shows a tiny thumb next
to the text no matter what. X still renders full-width
`summary_large_image` link cards, and robotics.arboryx.ai now serves
per-card server-rendered og pages (`/card/{card_id}` → 2400×1260 PNG with
a cache-busting `?g=<gcs-generation>` image URL), verified working on X
and in LinkedIn's Post Inspector.

So the winning strategy diverges per platform:

| Channel | Strategy | Why |
|---|---|---|
| X | **No media attached.** Deep link in text; X renders the og link card. | Proven working; link card is full-width and clickable. |
| LinkedIn | **Attach the card PNG natively** + deep link in the text. | Native image posts get the big view; link previews don't. |

Today the pipeline decides imagery **once per post, not per platform**
(`src/lib/funnel.py` — `let_platform_render_link_card` applies to every
channel the post fans out to). That single decision point is the gap.

## What changes

### 1. Imagery policy becomes per-channel

New tier.config key, additive and backward-compatible:

```
# Per-channel imagery. Values: link_card | attach | inherit (default).
# Unlisted channels fall back to the existing single-decision behavior
# (LET_PLATFORM_RENDER_LINK_CARD + imagery ladder).
IMAGERY_POLICY_X="link_card"
IMAGERY_POLICY_LINKEDIN="attach"
```

- `link_card` — never attach media on this channel; requires a deep link
  in the text (if the item has no deep link, fall back to `attach` so the
  post isn't naked).
- `attach` — run the existing imagery ladder (explicit → KG screenshot →
  entity graph → branded card → LLM image) and attach the result on this
  channel only.
- `inherit` / key absent — current behavior, unchanged. **Every existing
  tier keeps working with zero config edits.**

### 2. Where it lands in code (no new modules)

- `src/lib/config_loader.py` — parse `IMAGERY_POLICY_<CHANNEL>` keys into
  `tier.imagery_policy: dict[channel, str]`, inheriting parent → child
  like other keys.
- `src/lib/imagery.py` — `pick_image(...)` gains a `policy` argument;
  `let_platform_render_link_card` consults the per-channel policy instead
  of the global flag when a policy exists for the channel.
- `bin/daily.py` — already dispatches **one `create_post` per
  integration id**; pass the channel's resolved policy so the media list
  differs per call. Media is still uploaded **once** and reused for every
  `attach` channel.
- `bin/post.py` — currently fans out to all integrations in a single
  `create_post` call. When any per-channel policy is set for the tier,
  split into per-integration calls like daily.py (also removes the
  long-standing asymmetry between the two entry points).
- `src/lib/postiz_client.py` — no changes; it already supports media (or
  none) per call.

### 3. Content is still composed once

Text, hashtags, deep link, thread splitting: unchanged, one composition
per item (content cache still guarantees preview == publish). Only the
`image[]` list on each per-integration Postiz call differs.

### 4. Card PNG source for the robotics tier

The LinkedIn `attach` path for `arboryx.robotics` uses the KG card PNG.
Resolution order:

1. Local `../catalyst-knowledge-graph/data/exports/card_images/{card_id}.png`
   (bind-mounted sibling — the normal local case).
2. `https://robotics.arboryx.ai/card-img/{card_id}.png` (public, served by
   robotics-og from the private bucket) — download to
   `data/imagery_cache/`, hash-keyed like other imagery.

If neither resolves, LinkedIn falls back to `link_card` for that item
(post still goes out; compact thumb is the degraded mode, not a failure).

### 5. Manual queue + confirmation unchanged

Per-channel dispatch already isolates failures; `manual-post-queue.md`
entries now record which imagery policy applied so a human reposting by
hand knows whether to attach the image.

## Optional feature: KG confidence gate (ported from robotics-social)

The retired container selected cards by relationship confidence. Worth
keeping as an **opt-in** selection filter:

```
# Skip items whose max relationship confidence is below the threshold.
# OFF by default — daily.py keeps posting the newest unposted item.
CONFIDENCE_GATE_ENABLED="false"
CONFIDENCE_GATE_MIN="0.75"
```

- Applies only to tiers whose items carry a confidence signal (robotics
  cards: max relationship `confidence` from the Firestore doc /
  DuckDB join). Tiers without the signal ignore the gate.
- When ON, daily.py's newest-unposted scan skips below-threshold items
  (they stay unposted — a later confidence revision can resurrect them).
- Default **OFF**: cadence stays "1 newest item per tier per day",
  identical to today.

## Explicit non-goals

- **No fixing `tools/robotics-social`** in catalyst-knowledge-graph. It
  never spoke the real Postiz API (wrong endpoints, auth, payloads) and
  duplicates what this repo does properly. It stays dormant; the KG repo's
  `robotics-social-daily` Cloud Scheduler job stays paused. Retirement is
  a roadmap decision, not part of this change.
- No per-platform *text* divergence (same copy everywhere; only media
  differs).
- No analytics pull (was never implemented anywhere; separate ticket).
- Cloud/prod wiring. This design is local-first: cron + `make post` as
  today. If posting later moves to cloud, that's a separate design on top
  of this one.

## Acceptance checklist (when implemented)

- [ ] Tier without any `IMAGERY_POLICY_*` keys behaves byte-identically to
      today (regression: arboryx parent tier).
- [ ] robotics tier, X channel: post has deep link, **no** `image[]` in
      the Postiz payload; X renders the og card.
- [ ] robotics tier, LinkedIn channel: post has `image[]` with the card
      PNG (`{id, path}` pair) + deep link in text; LinkedIn shows the
      native image post.
- [ ] Missing PNG → LinkedIn degrades to link_card, run does not fail.
- [ ] `--dry-run`/preview shows the per-channel imagery decision.
- [ ] Confidence gate OFF: selection identical to today. ON with 0.75:
      low-confidence robotics cards skipped, not marked posted.
