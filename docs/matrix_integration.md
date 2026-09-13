# Matrix (Facades) — integration spec

**Status: not yet onboarded.** This is a design doc, not a runbook — Matrix has
no tier.config, no Postiz channels connected, no Cloud Run services, and
EdgeLane has no Matrix event publisher yet. It exists so the build can start
from an agreed shape instead of improvised mid-flight. Once real work starts,
promote the finished pieces into `products/facades/matrix_tier.config` and this
file becomes the operator's reference — same arc as **[simmer_integration.md](simmer_integration.md)**,
which *is* live and is the pattern this whole doc reuses.

Matrix trades a strategy grid (bull put, bear call, iron condor, iron fly,
bull call, bear put, call fly, put fly — see the reference screenshots below),
not a single ticker, so the event model differs from Simmer's watch/ready pair
in one real way: Simmer has 2 states, Matrix has ~6 candidate moments, most of
them optional ("post only when there's something worth sharing," not "post on
every tick"). Everything else — shared topic, per-product filtered push sub,
Cloud Run poster + snap pair, deterministic no-LLM copy, Firestore dedupe —
is the same mold.

## Model (vs. Simmer, for contrast)

| | Simmer | Matrix |
|---|---|---|
| Subject | one ticker's credit-spread readiness | a strategy grid (8 structures) scored + ranked each session |
| States that must post | 2, both mandatory (`watch_entered`, `ready`) | ~6, most **conditional on the engine judging the moment worth it** (§Gating) |
| Trigger | ticker crosses a readiness gate | engine pick changes, bias agrees/disagrees, win-rate crosses a notable bar, session opens, digest cadence, daily recap |
| Poster | `simmer-poster` (`bin/simmer_poster.py`) | `matrix-poster` (`bin/matrix_poster.py`, new — see §Build prerequisites) |
| Screenshot | 1 crop (`[data-snap="card"]` — GEX walls + gates + score) | multiple named crops, one per moment (§Screenshots) |
| Data source | EdgeLane read-only API, 1 endpoint | EdgeLane read-only API, per-moment endpoints (engine pick, bias, win-eval, grid) |
| Topic | `facades.ticker-events`, `attributes.product="simmer"` | **same topic**, `attributes.product="matrix"` (§GCP plan — this was an explicit call: only the engine knows when a pick is winning, so the signal has to come from the engine side, same as Simmer) |
| Config | `products/facades/simmer_tier.config` | `products/facades/matrix_tier.config` (placeholder already reserved in `facades_handles.example.yaml` / `config_loader.py`, not filled in) |

## Reference screenshots (from the actual Matrix UI)

Two panes the user shared ground the two most important crops:

- **The strategy grid** ("STRATEGIES (8)", each card: name + bias star,
  composite score, risk/legs tags, the price legs, Net/MaxP/MaxL/POP/EV,
  status pills — `HEALTHY` / `BROKEN` / `LIQ HIGH` / `LIQ MID` / `MARGINAL` /
  `TRADEABLE ON LIMIT` / `DO NOT TRADE` — and its limit-tier ladder). This is
  the "show the tool has real depth" post, not a daily one.
- **The engine-pick chip** ("ENGINE PICK — Bear Call · Aggressive · balanced",
  `COMPOSITE 86.1` + `TRADEABLE ON LIMIT` pills, Net prem/Max P/Max L/POP/EV,
  and — this is the important bit — it **already carries a plain-language
  annotation** ("Bias re-syncing — wait for a confirming win before sizing
  up."). That one pane does double duty: it's both the "engine picked
  something" shot *and* the "here's a flag explained in plain English" shot the
  brainstorm asked for separately. One crop, two post moments (§Post moments).

Two more moments were named in the brainstorm but aren't visible in either
screenshot yet — flagged as open items, not guessed at:
- **Win-evaluation grid** (win rate per strategy over time/trades) — no crop
  target exists; needs a UI view before `win_rate_notable` can ship.
- **Walls chip** (start-of-day chip, named after Simmer's GEX-wall crop) — same
  gap. The bias-chip (align/diverge vs. the engine pick) is *partially*
  visible as the ★ badge on the grid cards but isn't its own isolated crop
  yet either.

None of that blocks the spec — it just means `pick_selected` and
`daily_recap` can go live first (their crops exist today), the rest ship as
the Matrix UI grows its own `data-snap` targets.

## Screenshot capture (`matrix-snap`)

Same mechanism as `simmer-snap` (Cloud Run, headless Chromium, screenshots a
`[data-snap="…"]` element and returns PNG) — generalized to **multiple named
views** instead of one, since Matrix has multiple crops:

```
POST /snap  {"view": "engine_pick" | "strategy_grid" | "bias_chip" |
                     "walls_chip" | "win_eval_grid", ...params}
→ matrix UI loads /?snap=1&view=<view>[&...]
→ page shows only that one [data-snap="<view>"] wrapper (nav/toasts hidden,
  same convention Simmer's `?snap=1` already uses)
→ matrix-snap screenshots that element, returns PNG
```

This is also *why* the "don't give away all the data" goal in the brainstorm
is basically free: the crop is scoped to one named element server-side, on
the UI's own terms — the screenshot service (and the poster) never sees or
serves anything beyond what that `data-snap` wrapper renders. Same guarantee
Simmer already relies on; it isn't new machinery, just more `data-snap`
wrappers to author on the Matrix UI side.

`SNAP_SELECTOR` becomes per-view (`[data-snap="engine_pick"]`, etc.) rather
than the single hardcoded value `simmer-snap` uses today — a small, mechanical
change to `ops/simmer/snap/main.py`'s selector lookup (or its `matrix-snap`
fork — see §Build prerequisites) to key off the `view` param.

## Post moments

| state (`POST_ON_STATES`) | trigger | who decides it's worth posting | crop | cadence |
|---|---|---|---|---|
| `pick_selected` | engine's top strategy pick changes | **engine** — only fires on a real change, not every re-score | `engine_pick` | reactive, no cap needed (picks don't change that often) |
| `bias_aligned` / `bias_diverged` | the bias read agrees or disagrees with the engine's pick | **engine** — only when the relationship itself changed | `bias_chip` (open item — UI crop TBD) | reactive, optional |
| `win_rate_notable` | win rate on the evaluation grid crosses a meaningfully-higher bar | **engine only** — this is the one the brainstorm was explicit about: the poster has no way to know a pick is "winning," so this has to be an engine-published event on the shared topic, same as Simmer's `ready` | `win_eval_grid` (open item — UI crop TBD) | reactive, optional |
| `session_open` | start of trading day | **engine** — skip silently if there's nothing worth a walls-chip that day | `walls_chip` (open item — UI crop TBD) | at most 1/day |
| `grid_digest` | periodic full-grid share | **engine** — fires only when enough of the grid changed since the last digest | `strategy_grid` | target ~2×/week, engine-timed, not a cron |
| `daily_recap` | best/worst composite-score pick of the day | **engine** — plain-language "why," using the same tags shown in the `engine_pick` crop (BROKEN/HEALTHY/LIQ HIGH/MARGINAL/POP/EV) | `engine_pick` (best) [+ a second crop for the worst pick once one exists] | at most 1/day |

Every row publishes to the **same shared topic** (`facades.ticker-events`,
`attributes.product="matrix"`), the same way Simmer's `watch_entered`/`ready`
do — no new topic, no new subscription mechanics, just a new filtered sub
(`matrix-poster-sub`) and a longer `POST_ON_STATES` list. Start with just
`pick_selected` and `daily_recap` live (their crops exist); add the rest as
their UI views land — exactly the phased pattern Simmer itself used
(`POST_ON_STATES="watch_entered,ready"` was the whole list on day one too).

## Gating & anti-spam philosophy

This is the part of the brainstorm worth being explicit about, because it's a
policy decision, not a technical one: **the engine is the only thing that
decides a moment is worth a post.** The poster (`matrix-poster`) stays exactly
as dumb and deterministic as `simmer-poster` — it gates on `product` +
`state ∈ POST_ON_STATES`, dedupes on `event_id`, composes canned text, fetches
a crop, posts. It does not score "interestingness" itself; that judgment has
to sit upstream, in EdgeLane, because only the engine has the win-rate history,
the bias-vs-pick comparison, and the "did the grid change enough since the
last digest" state. This mirrors exactly why `win_rate_notable` has to be
engine-published rather than poller-computed.

The one thing worth adding on the poster side, as a safety net (not a
substitute for engine judgment): a per-state minimum gap
(`MATRIX_MIN_GAP_HOURS_<STATE>` or similar in the tier config) so a bug in the
engine's significance logic can't turn into a wall of posts. That's insurance,
not the primary gate — same relationship `POST_ON_STATES` already has to the
engine's own `state` choice.

Net effect, in the brainstorm's own words: not tweet-heavy, a drift of
genuinely informative posts, each one earning its place by showing something
true and specific enough that a reader wants to see the rest — without ever
handing over the full tool for free (the scoped `data-snap` crop already
enforces that mechanically, see §Screenshots).

## Text/copy

Same rule as Simmer's `compose_simmer()`: **deterministic, no LLM rewrite** —
the post must equal the engine's snapshot at publish time. A `compose_matrix()`
in `src/lib/recipes.py` would need one template per state, e.g.:

- `pick_selected` → `"Engine pick: {strategy}. Composite {score}. {tag_1}, {tag_2}…"`
- `daily_recap` → `"Best setup today: {strategy} (composite {score}). {plain_language_why}."` where `plain_language_why` comes from a small **tag → plain-English clause** lookup, not free text:
  - `BROKEN` → "the model's edge assumption didn't hold up"
  - `HEALTHY` + `LIQ HIGH` → "liquidity's deep enough to size into"
  - `MARGINAL` → "on the edge — thin liquidity or a slim edge, worth a second look before sizing up"
  - `TRADEABLE ON LIMIT` → "workable, but only at a limit price, not the market"
  - `DO NOT TRADE` → "the engine is flagging this one to sit out"

  This keeps the "high-level, non-technical, easy to understand" requirement
  from the brainstorm honest: it's a lookup table, not a paraphrase model, so
  the copy can never drift from what the tags actually say.
- `grid_digest` → a fixed caption ("This week's strategy grid — N setups
  scored, M tradeable.") ; the grid crop carries the real information.

Never more than the post moments in the table above — no filler post to hit a
cadence target.

## GCP plan (dependencies, component by component)

Reusing everything Simmer already stood up wherever the shape matches
exactly, standing up only what's genuinely per-product:

| Component | Simmer (live) | Matrix (planned) | Reuse? |
|---|---|---|---|
| Pub/Sub topic | `facades.ticker-events` | **same topic** | ✅ reuse, no change |
| Push subscription | `simmer-poster-sub`, filter `attributes.product="simmer"` | `matrix-poster-sub`, filter `attributes.product="matrix"` | new sub, same topic, same `deploy.sh` mechanics |
| Runtime SA | `facades-poster-sa` | **same SA** | ✅ reuse — add `run.invoker` on the two new services + `secretAccessor` on `matrix-api-token` |
| Deployer SA | `market-agent-sa` | **same SA** | ✅ reuse |
| Poster service | `simmer-poster` (`bin/simmer_poster.py`) | `matrix-poster` (`bin/matrix_poster.py` — new file; see §Build prerequisites, `bin/simmer_poster.py` is Simmer-specific by name, not a generic multi-tenant poster) | new code, same skeleton |
| Snap service | `simmer-snap` (`ops/simmer/snap/`, 1 fixed selector) | `matrix-snap` (new; multi-view selector, §Screenshots) | new code, same skeleton |
| Dedupe store | Firestore `simmer_poster_dedupe` | Firestore `matrix_poster_dedupe` | ✅ `deploy.sh` already parameterizes this as `${PRODUCT}_poster_dedupe` — no change needed |
| Source adapter | `SimmerAPI` (`src/lib/sources/simmer_source.py`) | `MatrixAPI` (new; per-moment endpoints, not one) | new code |
| Recipe/copy | `compose_simmer()` | `compose_matrix()` (§Text/copy) | new code |
| Secret | `simmer-api-token` | `matrix-api-token` | new secret, same pattern |
| Tier config | `products/facades/simmer_tier.config` | `products/facades/matrix_tier.config` | new file — placeholder line already exists (commented) in `_TIER_FILE_BY_ID`, `facades_handles.example.yaml` |
| Postiz channels | LinkedIn Simmer page, X `@facades_simmer` — both live | LinkedIn "Matrix" page, X `@facades_matrix` — both **pending**, ids reserved in `facades_handles.example.yaml` only | must connect in Postiz before `matrix-poster` can publish anything |
| Preflight | `make simmer-preflight` (`ops/simmer/preflight.sh simmer`) | `ops/simmer/preflight.sh matrix` once the tier is registered | ✅ already product-parameterized, no change needed |

Illustrative `matrix_tier.config` skeleton (not created yet — for shape only):

```
TIER_ID="matrix"
TIER_NAME="Matrix"
DATA_SOURCE_1_TYPE="matrix_api"
DATA_SOURCE_1_BASE_URL="${MATRIX_API_BASE}"          # e.g. https://edge.facades.trade (or its own host)
DATA_SOURCE_1_TOKEN_ENV="MATRIX_API_TOKEN"

MATRIX_PUBSUB_PROJECT="${GCP_PROJECT}"
MATRIX_PUBSUB_SUBSCRIPTION="${MATRIX_PUBSUB_SUBSCRIPTION}"   # matrix-poster-sub
POST_ON_STATES="pick_selected,daily_recap"           # start narrow; widen as UI crops land

CHANNEL_LINKEDIN="${POSTIZ_INTEGRATION_ID_LINKEDIN_MATRIX}"
CHANNEL_X_PRIMARY="${POSTIZ_INTEGRATION_ID_X_MATRIX}"
POSTIZ_CUSTOMER_ID="${POSTIZ_CUSTOMER_ID_MATRIX}"      # same "Facades" customer as Simmer, if shared

IMAGERY_POLICY_X="attach"
IMAGERY_POLICY_LINKEDIN="attach"
CARD_RENDER_PROVIDER="matrix_snap"
MATRIX_SNAP_URL="${MATRIX_SNAP_URL}"

HANDLE_INJECTION="false"                             # same lesson as Simmer's cashtag bug —
CASHTAGS_ENABLED="false"                             # compose_matrix() should own its own tags/handles,
ENTITY_TAG_MODE="cashtag_only"                        # not the per-channel dispatcher, unless a real
MAX_ENTITY_TAGS="0"                                   # need for it shows up.

PARENT_URL_TEMPLATE="https://matrix.facades.trade/"
POSTING_CADENCE_DAILY="false"                         # event-driven, no scheduler row
```

## Build prerequisites (what has to exist before this can go live)

**EdgeLane side (Matrix engine + UI) — none of this exists yet:**
1. Publish the 6 states in §Post moments to `facades.ticker-events`,
   `attributes.product="matrix"` — same publisher contract Simmer's engine
   already implements (`event_id`, deterministic per day+state+scope, e.g.
   `MTX-BEARCALL-260913-pick_selected`).
2. A read-only API (`edge.facades.trade` or Matrix's own host) exposing the
   data each crop/copy template needs per moment — Simmer has one endpoint
   (`GET /simmer/state/<SYM>`); Matrix likely needs one per moment (engine
   pick, bias comparison, win-eval, grid summary).
3. `?snap=1&view=<name>` render mode + a `[data-snap="<view>"]` wrapper per
   crop in the Matrix UI — `engine_pick` and `strategy_grid` can be built from
   the two screenshots already in hand; `bias_chip`, `walls_chip`,
   `win_eval_grid` need their views designed first.

**This repo — small, mechanical, but not yet done:**
1. `bin/matrix_poster.py` — copy `bin/simmer_poster.py`'s skeleton
   (`TIER_ID`, dedupe, `process_event`, `run_pull`/`run_serve`/`create_app`),
   swap in `MatrixAPI` / `compose_matrix()` / `source_type="matrix_api"`.
   `bin/simmer_poster.py` is Simmer-specific by name throughout — it isn't a
   generic multi-tenant poster today, so this is a new file, not a flag.
2. `ops/matrix/poster/` + `ops/matrix/snap/` (Dockerfile, cloudbuild.yaml) —
   `ops/simmer/deploy.sh` currently hardcodes the source paths
   `ops/simmer/poster` / `ops/simmer/snap` for **every** product it deploys
   (only the Cloud Run *names* and env var values are `$PRODUCT`-parameterized
   today). Either generalize `deploy.sh` to take a source-dir per product, or
   fork it — needs a decision before `make matrix-deploy` can exist.
3. `deploy.sh` also hardcodes `SIMMER_API_BASE=https://edge.facades.trade` and
   several `SIMMER_`-prefixed env var *names* (not just values) when wiring the
   poster's env — those need to become product-neutral (or the script needs a
   per-product env-var map) before it can stand up `matrix-poster` correctly.
4. `src/lib/sources/matrix_source.py` (`MatrixAPI`, mirrors `simmer_source.py`),
   `compose_matrix()` in `src/lib/recipes.py`, the `matrix_api` branch in
   `src/lib/sources/factory.py`.
5. Multi-view support in the snap service — `ops/simmer/snap/main.py` reads one
   fixed `SNAP_SELECTOR`; the Matrix fork needs to select by the `view` param
   (§Screenshots).
6. `_TIER_FILE_BY_ID["matrix"]` uncommented in `src/lib/config_loader.py`
   (the line is already there, commented out).

**Postiz / ops:**
1. Connect X `@facades_matrix` and the LinkedIn "Matrix" page as channels;
   fill in the real `POSTIZ_INTEGRATION_ID_*_MATRIX` ids (placeholders only in
   `facades_handles.example.yaml` today).
2. `matrix-api-token` secret in Secret Manager.
3. `make simmer-preflight` (or its `matrix` invocation once the tier exists)
   before the first live event.

None of this touches Simmer — same guarantee `simmer_integration.md` already
states for adding a new product: the shared topic and `facades-poster-sa`
don't change, Matrix just adds its own filtered subscription and services.

## Open questions (for whoever green-lights the build)

- Where does the win-evaluation grid live, and what does "notable" win-rate
  mean numerically? That threshold decides `win_rate_notable` — engine-side,
  not something this repo can define.
- Is the "walls chip" a genuinely new UI element, or a relabeled version of
  something that already exists in the Matrix UI under another name?
- Same Postiz "Facades" customer as Simmer, or its own? (Affects whether
  `POSTIZ_CUSTOMER_ID_MATRIX` is a new value or reuses Simmer's.)
- Confirm `@facades_matrix` / LinkedIn "Matrix" are still the intended
  handles before connecting them (placeholders have sat unconnected in
  `facades_handles.example.yaml` since Simmer's onboarding).
