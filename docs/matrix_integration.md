# Matrix (Facades) — integration spec

**Status: not yet onboarded on the Postiz side.** Matrix has no tier.config,
no Postiz channels connected, no Cloud Run services. But unlike a from-scratch
product, **the Matrix engine and UI already exist in EdgeLane** — the strategy
grid, the bias engine, the win/loss evaluator, the dealer-exposure walls —
none of that is hypothetical (`market/backend/app/{strategy_engine,bias_engine,
evaluator,dealer_exposures,accuracy}.py`, `market/ui/`). What's missing is the
**Postiz-facing layer**: an event publisher, a read-only API, and snapshot
render endpoints — exactly the three things Simmer's engine had to add on top
of its own already-working readiness logic. That EdgeLane-side spec now has
its own doc: **`EdgeLane/docs/matrix_events_update.md`** (written for Postiz
first; the engine builds against it when ready). This file is the Postiz-side
half of the same plan.

Once real work starts, promote the finished pieces into
`products/facades/matrix_tier.config` and this file becomes the operator's
reference — same arc as **[simmer_integration.md](simmer_integration.md)**,
which *is* live and is the pattern this whole doc reuses, **with one
deliberate difference: Matrix does not share Simmer's topic or its poster/snap
containers** (§GCP plan) — only the two service accounts and the GCP project's
Pub/Sub service are shared.

Matrix trades a strategy grid (bull put, bear call, iron condor, iron fly,
bull call, bear put, call fly, put fly — see the reference screenshots below)
for one symbol at a time — the engine's own state is per-symbol
(`evaluator_state.consec_wins_by_symbol`, `poller_state.latest_by_symbol`,
etc.), same as Simmer, it just scores 8 candidate structures instead of one
readiness gate. The event model differs from Simmer's watch/ready pair in one
real way: Simmer has 2 mandatory states, Matrix has 6 candidate moments, most
of them optional ("post only when there's something worth sharing," not "post
on every tick"). Everything else — filtered push sub, Cloud Run poster + snap
pair, deterministic no-LLM copy, Firestore dedupe — is the same mold.

## Model (vs. Simmer, for contrast)

| | Simmer | Matrix |
|---|---|---|
| Subject | one ticker's credit-spread readiness | one ticker's 8-strategy grid, scored + ranked each session |
| States that must post | 2, both mandatory (`watch_entered`, `ready`) | 6, most **conditional on the engine judging the moment worth it** (§Gating) |
| Trigger | ticker crosses a readiness gate | engine pick changes, bias agrees/disagrees, win-rate crosses a notable bar, session opens, digest cadence, daily recap |
| Poster | `simmer-poster` (`bin/simmer_poster.py`) | `matrix-poster` (`bin/matrix_poster.py`, new — see §Build prerequisites) |
| Screenshot | 1 crop (`[data-snap="card"]`, bearer-authed render endpoint) | 5 named crops, same bearer-authed render pattern (§Screenshots) |
| Data source | EdgeLane read-only API, 1 endpoint | EdgeLane read-only API, per-moment endpoints (engine pick, bias, win-eval, grid) |
| Pub/Sub topic | `facades.ticker-events`, `attributes.product="simmer"` | **its own topic**, `facades.matrix-events` — deliberately NOT shared with Simmer (§GCP plan) |
| Containers | `simmer-poster` + `simmer-snap` | `matrix-poster` + `matrix-snap` — **their own images**, not the Simmer ones re-pointed |
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
  up."). That annotation is EdgeLane's real `_HINT_TEXT` from
  `app/routes/accuracy.py` — the bias-trust surface, not a mockup string. That
  one pane does double duty: it's both the "engine picked something" shot
  *and* the "here's a flag explained in plain English" shot the brainstorm
  asked for separately. One crop, two post moments (§Post moments).

The other three moments map onto engine internals that already exist, even
though no screenshot shows their UI yet:

- **Win-evaluation grid** → `evaluator.py`'s per-symbol `consec_wins`/
  `consec_losses`/`regime_alert_active` counters and `accuracy.py`'s rolling
  `win_rate`/`graded` fields — real, already computed every ~30s. Only the
  crop is missing.
- **Walls chip** → confirmed a genuinely separate, standalone chip (not a
  relabeling of anything) — `dealer_exposures.py`'s `key_levels: {call_wall,
  put_wall, vex_wall, tex_wall}` is exactly this data. Only the crop is
  missing.
- **Bias chip** (align/diverge vs. the engine pick) → `accuracy.py`'s
  bias-trust `state` field itself (`in_sync` | `low_conf` | `calibrating` |
  `paused`) already *is* this signal. Only the crop is missing.

None of that blocks the spec — it just means `pick_selected` and
`daily_recap` can go live first (their crops exist today via the engine-pick
chip), the rest ship as their `data-snap` views land in the Matrix UI. Full
detail on wiring each of these into an event: `EdgeLane/docs/matrix_events_update.md`.

## Screenshot capture (`matrix-snap`)

Follows the corrected Simmer pattern exactly (§ "the fix" in
`simmer_integration.md`'s history: the snap service must **never** hit the
live, login-gated SPA — a headless browser has no session and would only ever
capture the sign-in dialog. It hits a dedicated, server-rendered, bearer-authed
endpoint instead) — generalized to multiple named views since Matrix has
multiple crops:

```
POST /snap  {"symbol": "NVDA", "view": "engine_pick" | "strategy_grid" |
                                        "bias_chip" | "walls_chip" | "win_eval_grid"}
→ matrix-snap sets Authorization: Bearer $MATRIX_API_TOKEN
→ GET https://edge.facades.trade/matrix/snap/<SYM>?view=<view>
   (standalone HTML card, inline CSS, no SPA, no user session —
    app/matrix_snap.py::render_snap_card mirroring Simmer's own)
→ screenshot [data-snap="<view>"], return PNG
```

This is also *why* the "don't give away all the data" goal in the brainstorm
is basically free: the crop is scoped to one named element server-side, on
the render endpoint's own terms — the screenshot service (and the poster)
never sees or serves anything beyond what that `data-snap` wrapper renders.
Same guarantee Simmer's corrected snap service now relies on.

`SNAP_SELECTOR` becomes per-view (`[data-snap="engine_pick"]`, etc.) selected
by the `view` param, in `matrix-snap`'s own `main.py` — a fork of
`ops/simmer/snap/main.py`, not a shared code path (§GCP plan).

## Post moments

| state (`POST_ON_STATES`) | trigger | who decides it's worth posting | crop | cadence |
|---|---|---|---|---|
| `pick_selected` | engine's top strategy pick changes | **engine** — only fires on a real change, not every re-score | `engine_pick` | reactive, no cap needed (picks don't change that often) |
| `bias_aligned` / `bias_diverged` | `accuracy.py`'s bias-trust `state` transitions (e.g. `in_sync` ↔ `paused`/`low_conf`) | **engine** — only when the relationship itself changed | `bias_chip` | reactive, optional |
| `win_rate_notable` | a recovery pattern (loss streak → win) or a high-win-frequency stretch on the eval grid — **not a daily obligation** | **engine only** — the poster has no way to know a pick is "winning"; see §Gating and `matrix_events_update.md` for the exact rule | `win_eval_grid` | reactive, optional, no cadence floor or ceiling — fires only when the pattern matches |
| `session_open` | start of trading day | **engine** — skip silently if there's nothing worth a walls-chip that day | `walls_chip` | at most 1/day |
| `grid_digest` | periodic full-grid share | **engine** — fires only when enough of the grid changed since the last digest | `strategy_grid` | target ~2×/week, engine-timed, not a cron |
| `daily_recap` | best/worst composite-score pick of the day | **engine** — plain-language "why," using the same tags shown in the `engine_pick` crop (BROKEN/HEALTHY/LIQ HIGH/MARGINAL/POP/EV) | `engine_pick` (best) [+ a second crop for the worst pick once one exists] | at most 1/day |

Every row publishes to Matrix's **own** topic (`facades.matrix-events`,
`attributes.product="matrix"`) — a new filtered sub (`matrix-poster-sub`) on
that topic, not a share of Simmer's. Start with just `pick_selected` and
`daily_recap` live (their crops exist); add the rest as their UI views land —
the same phased pattern Simmer itself used (`POST_ON_STATES="watch_entered,ready"`
was the whole list on day one too).

## Gating & anti-spam philosophy

This is the part of the brainstorm worth being explicit about, because it's a
policy decision, not a technical one: **the engine is the only thing that
decides a moment is worth a post.** The poster (`matrix-poster`) stays exactly
as dumb and deterministic as `simmer-poster` — it gates on `product` +
`state ∈ POST_ON_STATES`, dedupes on `event_id`, composes canned text, fetches
a crop, posts. It does not score "interestingness" itself; that judgment has
to sit upstream, in EdgeLane, because only the engine has the win-rate
history, the bias-vs-pick comparison, and the "did the grid change enough
since the last digest" state.

**`win_rate_notable`, concretely** (per the brainstorm's own framing — no
daily obligation, only fire on something earned): the engine already runs a
periodic outcome-grading sweep (`evaluator.py`, every ~30s, updating
`consec_wins`/`consec_losses`/`regime_alert_active` per symbol as trades
grade). That sweep is the natural place to also ask "did a postable pattern
just appear" — e.g. a regime alert clearing (a loss streak just recovered
into a win) or `win_rate` crossing into a materially better tier — right
after it updates those counters, since it already has the before/after state
needed to detect the transition. **Recommendation: augment that existing
sweep, don't add a separate watcher.** A standalone watcher would just re-read
the same state a moment later on its own clock — redundant infra for no
benefit, and a second place that can drift out of sync with the grading
logic. The one thing worth doing to keep this safe: make the Pub/Sub publish
call inside the sweep best-effort and non-blocking (mirror `simmer_events.py`'s
own posture — log and swallow, never raise) so a transient publish failure
can never stall grading. Full rule detail: `matrix_events_update.md`.

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

**What's shared with Simmer, and what deliberately is not:** the two service
accounts and the GCP project's Pub/Sub *service* are shared (there's only one
of each per project). The **topic** and **every container** are Matrix's own
— this was an explicit correction to the first draft of this doc, which had
assumed Matrix would reuse Simmer's topic and images.

| Component | Simmer (live) | Matrix (planned) | Reuse? |
|---|---|---|---|
| Pub/Sub topic | `facades.ticker-events` | **`facades.matrix-events`** — its own topic | ❌ new topic, deliberately not shared |
| Push subscription | `simmer-poster-sub`, filter `attributes.product="simmer"` | `matrix-poster-sub`, filter `attributes.product="matrix"`, on the new topic | new sub, new topic |
| Runtime SA | `facades-poster-sa` | **same SA** | ✅ reuse — add `run.invoker` on the two new services + `secretAccessor` on `matrix-api-token` |
| Deployer SA | `market-agent-sa` | **same SA** | ✅ reuse |
| Poster service | `simmer-poster` (`bin/simmer_poster.py`, `ops/simmer/poster/`) | `matrix-poster` — **its own image**, `bin/matrix_poster.py` + `ops/matrix/poster/` | ❌ new code, new container, same skeleton |
| Snap service | `simmer-snap` (`ops/simmer/snap/`, 1 fixed selector) | `matrix-snap` — **its own image**, `ops/matrix/snap/`, multi-view selector (§Screenshots) | ❌ new code, new container, same skeleton |
| Dedupe store | Firestore `simmer_poster_dedupe` | Firestore `matrix_poster_dedupe` | naming convention only — same Firestore instance, different collection |
| Source adapter | `SimmerAPI` (`src/lib/sources/simmer_source.py`) | `MatrixAPI` (new; per-moment endpoints, not one) | new code |
| Recipe/copy | `compose_simmer()` | `compose_matrix()` (§Text/copy) | new code |
| Secret | `simmer-api-token` | `matrix-api-token` | new secret, same pattern |
| Tier config | `products/facades/simmer_tier.config` | `products/facades/matrix_tier.config` | new file — placeholder line already exists (commented) in `_TIER_FILE_BY_ID`, `facades_handles.example.yaml` |
| Postiz channels | LinkedIn Simmer page, X `@facades_simmer` — both live | LinkedIn "Matrix" page, X `@facades_matrix` — handles confirmed, connection still **pending** | must connect in Postiz before `matrix-poster` can publish anything |
| Deploy script | `ops/simmer/deploy.sh` | **`ops/matrix/deploy.sh`** — its own script (§Build prerequisites: forked, not a generalized shared script) | ❌ forked, not shared |
| Preflight | `ops/simmer/preflight.sh simmer` | `ops/matrix/preflight.sh matrix` (a copy, same checks re-pointed at Matrix's own topic/services) | fork alongside `deploy.sh`, same checks |

Illustrative `matrix_tier.config` skeleton (not created yet — for shape only):

```
TIER_ID="matrix"
TIER_NAME="Matrix"
DATA_SOURCE_1_TYPE="matrix_api"
DATA_SOURCE_1_BASE_URL="${MATRIX_API_BASE}"          # e.g. https://edge.facades.trade (or its own host)
DATA_SOURCE_1_TOKEN_ENV="MATRIX_API_TOKEN"

MATRIX_PUBSUB_PROJECT="${GCP_PROJECT}"
MATRIX_PUBSUB_TOPIC="facades.matrix-events"          # Matrix's own topic — not Simmer's
MATRIX_PUBSUB_SUBSCRIPTION="${MATRIX_PUBSUB_SUBSCRIPTION}"   # matrix-poster-sub
POST_ON_STATES="pick_selected,daily_recap"           # start narrow; widen as UI crops land

CHANNEL_LINKEDIN="${POSTIZ_INTEGRATION_ID_LINKEDIN_MATRIX}"
CHANNEL_X_PRIMARY="${POSTIZ_INTEGRATION_ID_X_MATRIX}"
POSTIZ_CUSTOMER_ID="${POSTIZ_CUSTOMER_ID_MATRIX}"      # default: same "Facades" customer as Simmer

IMAGERY_POLICY_X="attach"
IMAGERY_POLICY_LINKEDIN="attach"
CARD_RENDER_PROVIDER="matrix_snap"
MATRIX_SNAP_URL="${MATRIX_SNAP_URL}"

HANDLE_INJECTION="false"                             # same lesson as Simmer's cashtag bug —
CASHTAGS_ENABLED="false"                             # compose_matrix() should own its own tags/handles,
ENTITY_TAG_MODE="cashtag_only"                        # not the per-channel dispatcher, unless a real
MAX_ENTITY_TAGS="0"                                   # need for it shows up.

PARENT_URL_TEMPLATE="https://matrix.facades.trade/?symbol={symbol}"
POSTING_CADENCE_DAILY="false"                         # event-driven, no scheduler row
```

## Build prerequisites (what has to exist before this can go live)

**EdgeLane side — the engine/UI already exist; the Postiz-facing layer does
not.** Full spec: `EdgeLane/docs/matrix_events_update.md`. Summary:
1. A `matrix_events.py` publisher (mirrors `simmer_events.py`) publishing the
   6 states in §Post moments to **`facades.matrix-events`** (its own topic,
   not Simmer's), with a deterministic `event_id` per (symbol, day, state) —
   e.g. `MTX-<SYM>-<YYMMDD>-<state>`.
2. A read-only API (`edge.facades.trade` or Matrix's own host) exposing the
   data each crop/copy template needs per moment — Simmer has one endpoint
   (`GET /simmer/state/<SYM>`); Matrix needs one per moment (engine pick,
   bias, win-eval, grid summary), grounded in `evaluator.py` / `accuracy.py` /
   `dealer_exposures.py` state that already exists.
3. `GET /matrix/snap/<SYM>?view=<name>` — a dedicated, bearer-authed,
   server-rendered standalone HTML card per view (mirrors Simmer's corrected
   `/simmer/snap/<SYM>` — **never** the live, login-gated SPA), with a
   `[data-snap="<view>"]` wrapper per crop. `engine_pick` and `strategy_grid`
   can be built from the two screenshots already in hand; `bias_chip`,
   `walls_chip`, `win_eval_grid` render the data already computed by
   `accuracy.py` / `dealer_exposures.py` / the eval grid respectively.
4. `win_rate_notable`'s exact firing rule (recovery pattern / high-frequency
   pattern), and the decision to compute it inline in the existing evaluator
   sweep rather than a separate watcher (§Gating) — written up in
   `matrix_events_update.md` for the engine team to build against.

**This repo — new code, deliberately not a fork/flag of Simmer's:**
1. `bin/matrix_poster.py` — copy `bin/simmer_poster.py`'s skeleton
   (`TIER_ID`, dedupe, `process_event`, `run_pull`/`run_serve`/`create_app`),
   swap in `MatrixAPI` / `compose_matrix()` / `source_type="matrix_api"` /
   `facades.matrix-events`.
2. `ops/matrix/poster/` + `ops/matrix/snap/` (Dockerfile, cloudbuild.yaml) —
   **its own directories and images**, not `ops/simmer/{poster,snap}` re-used.
3. `ops/matrix/deploy.sh` — **forked from `ops/simmer/deploy.sh`**, not a
   generalization of it. `ops/simmer/deploy.sh` hardcodes `ops/simmer/poster`
   / `ops/simmer/snap` as source paths and several `SIMMER_`-prefixed env var
   *names* (not just values, e.g. `SIMMER_API_BASE=https://edge.facades.trade`
   is baked into `deploy_poster()`) — those assumptions are specific enough to
   Simmer's shape that a fork is cleaner than threading a product-neutral
   config map through the existing script. `ops/matrix/deploy.sh` targets the
   Matrix directories/images/topic/env-var names directly. Corresponding
   `Makefile` targets: `matrix-deploy`, `matrix-poster`, `matrix-preflight`,
   etc., mirroring the `simmer-*` ones but pointed at `ops/matrix/`.
4. `src/lib/sources/matrix_source.py` (`MatrixAPI`, mirrors `simmer_source.py`),
   `compose_matrix()` in `src/lib/recipes.py`, the `matrix_api` branch in
   `src/lib/sources/factory.py`.
5. Multi-view support in `ops/matrix/snap/main.py` (its own fork of
   `ops/simmer/snap/main.py`) — select `SNAP_SELECTOR` by the `view` param
   (§Screenshots).
6. `_TIER_FILE_BY_ID["matrix"]` uncommented in `src/lib/config_loader.py`
   (the line is already there, commented out).

**Postiz / ops:**
1. Connect X `@facades_matrix` and the LinkedIn "Matrix" page as channels
   (handles confirmed); fill in the real `POSTIZ_INTEGRATION_ID_*_MATRIX` ids
   (placeholders only in `facades_handles.example.yaml` today).
2. `matrix-api-token` secret in Secret Manager.
3. `make matrix-preflight` (once `ops/matrix/preflight.sh` exists) before the
   first live event.

None of this touches Simmer — its topic, its containers, its images are
untouched by any of the above.

## Open questions (for whoever green-lights the build)

- Same Postiz "Facades" customer as Simmer, or its own? Defaulted to "same"
  above (`POSTIZ_CUSTOMER_ID_MATRIX` reuses the value) — cheap to change
  later if wrong, so not blocking.
