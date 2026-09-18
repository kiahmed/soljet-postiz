# Matrix (Facades) — integration spec

**Status: live as of 2026-09-13.** Both sides shipped: this repo's tier,
poster, and snap service, and EdgeLane's event publisher (`app/matrix_events.py`
+ `app/matrix_signals.py`), read-only API (`app/routes/matrix.py`), and
snapshot endpoints (`app/matrix_snap.py`) — see `EdgeLane/docs/
matrix_events_update.md`'s "Status — shipped" table. All 7 states
(`POST_ON_STATES`) are live, all 5 snap views resolve, and `bin/matrix_poster.py`
composes from real engine data (not the minimal-card fallback) for a tracked
symbol. Matrix currently covers **SPX and NDX only** — not arbitrary tickers
like Simmer.

The Matrix engine and UI predate this integration — the strategy grid, the
bias engine, the win/loss evaluator, the dealer-exposure walls were already
working (`market/backend/app/{strategy_engine,bias_engine,evaluator,
dealer_exposures,accuracy}.py`, `market/ui/`). What this doc and
`matrix_events_update.md` added was the **Postiz-facing layer**: an event
publisher, a read-only API, and snapshot render endpoints — the same three
things Simmer's engine had to add on top of its own already-working
readiness logic.

This file stays the operator's reference — same arc as
**[simmer_integration.md](simmer_integration.md)**, which *is* live and is
the pattern this whole doc reuses, **with one
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
| Subject | any ticker's credit-spread readiness | **SPX/NDX only** (as of this writing) — one 8-strategy grid, scored + ranked each session |
| States that must post | 2, both mandatory (`watch_entered`, `ready`) | 7 (6 moments — bias splits into 2), all live; the engine's own significance rules are the real gate, a min-gap floor on 3 of them is the safety net (§Gating) |
| Trigger | ticker crosses a readiness gate | engine pick changes, bias agrees/disagrees, win-rate crosses a notable bar, session opens, digest cadence, daily recap |
| Poster | `simmer-poster` (`bin/simmer_poster.py`) | `matrix-poster` (`bin/matrix_poster.py`) |
| Screenshot | 1 crop (`[data-snap="card"]`, bearer-authed render endpoint) | 5 named crops, same bearer-authed render pattern (§Screenshots) — all 5 confirmed live |
| Data source | EdgeLane read-only API, 1 endpoint | EdgeLane read-only API, 5 blocks (`pick`/`bias`/`grid`/`win_eval`/`walls`) fetched per event |
| Pub/Sub topic | `facades.ticker-events`, `attributes.product="simmer"` | **its own topic**, `facades.matrix-events` — deliberately NOT shared with Simmer (§GCP plan) |
| Containers | `simmer-poster` + `simmer-snap` | `matrix-poster` + `matrix-snap` — their own images, deployed and live |
| Config | `products/facades/simmer_tier.config` | `products/facades/matrix_tier.config` — live |

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

The other three moments map onto the same engine internals — all confirmed
live via `GET /matrix/snap/<SYM>?view=<name>` as of 2026-09-13:

- **Win-evaluation grid** → `evaluator.py`'s per-symbol `consec_wins`/
  `consec_losses`/`regime_alert_active` counters and `accuracy.py`'s rolling
  `win_rate`/`graded` fields.
- **Walls chip** → `dealer_exposures.py`'s `key_levels: {call_wall, put_wall,
  vex_wall, tex_wall, gex_wall}`.
- **Bias chip** (align/diverge vs. the engine pick) → `accuracy.py`'s
  bias-trust `state` field itself (`in_sync` | `low_conf` | `calibrating` |
  `paused`).

Full detail on how each was wired into an event: `EdgeLane/docs/
matrix_events_update.md`'s "Status — shipped" table.

## Screenshot capture (`matrix-snap`)

Follows the corrected Simmer pattern exactly (§ "the fix" in
`simmer_integration.md`'s history: the snap service must **never** hit the
live, login-gated SPA — a headless browser has no session and would only ever
capture the sign-in dialog. It hits a dedicated, server-rendered, bearer-authed
endpoint instead) — generalized to multiple named views since Matrix has
multiple crops:

```
POST /snap  {"symbol": "SPX", "view": "engine_pick" | "strategy_grid" |
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
| `pick_selected` | engine's top strategy pick changes | **engine** — only fires on a real change, not every re-score | `engine_pick` | reactive; `MATRIX_MIN_GAP_HOURS_PICK_SELECTED` floor added 2026-09-17 as a backstop (see §Gating incident note) |
| `bias_aligned` / `bias_diverged` | `accuracy.py`'s bias-trust `state` transitions (e.g. `in_sync` ↔ `paused`/`low_conf`) | **engine** — only when the relationship itself changed | `bias_chip` | reactive, optional |
| `win_rate_notable` | a recovery pattern (loss streak → win) or a high-win-frequency stretch on the eval grid — **not a daily obligation** | **engine only** — the poster has no way to know a pick is "winning"; see §Gating and `matrix_events_update.md` for the exact rule | `win_eval_grid` | reactive, optional, no cadence floor or ceiling — fires only when the pattern matches |
| `session_open` | start of trading day | **engine** — skip silently if there's nothing worth a walls-chip that day | `walls_chip` | at most 1/day |
| `grid_digest` | periodic full-grid share | **engine** — fires only when enough of the grid changed since the last digest | `strategy_grid` | target ~2×/week, engine-timed, not a cron |
| `daily_recap` | best/worst composite-score pick of the day | **engine** — plain-language "why," using the same tags shown in the `engine_pick` crop (BROKEN/HEALTHY/LIQ HIGH/MARGINAL/POP/EV); the worst pick is a real second line now (lowest `composite_score` in the `grid` block), not a TBD | `engine_pick` (best) | at most 1/day (`MATRIX_MIN_GAP_HOURS_DAILY_RECAP=20`) |

All 7 states are live in `POST_ON_STATES` as of 2026-09-13 — every row
publishes to Matrix's **own** topic (`facades.matrix-events`,
`attributes.product="matrix"`), on the filtered sub `matrix-poster-sub`, not
a share of Simmer's.

## Market hours

`bin/matrix_poster.py` gates every event on the regular US session
(9:30–16:00 America/New_York, Mon–Fri — `src/lib/market_hours.py`). Unlike
Simmer, **there is no off-hours exception** — a multi-leg spread read against
a stale chain has no "last available snapshot + catalyst" fallback story the
way a single-ticker credit-spread call does, so an off-hours event is always
skipped, full stop. `MARKET_HOURS_ENFORCED="false"` in `matrix_tier.config`
disables the gate entirely (testing only).

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

**Shipped**: a per-state minimum gap (`MATRIX_MIN_GAP_HOURS_<STATE>` in
`matrix_tier.config` — `SESSION_OPEN`/`GRID_DIGEST`/`DAILY_RECAP`, plus
`PICK_SELECTED` as of 2026-09-17; the other reactive states still have no
floor since they already only fire on a real transition) is a safety net,
not a substitute for engine judgment, so a bug in the engine's significance
logic can't turn into a wall of posts. Enforced in
`bin/matrix_poster.py::process_event()` via `Dedupe.last_state_time()` /
`mark_state_time()` — same relationship `POST_ON_STATES` already has to the
engine's own `state` choice: insurance, not the primary gate. A state that's
gapped out is `status: "skipped"`, acked (204) like any other skip — Pub/Sub
doesn't hold or retry it. Note this floor is global across symbols (keyed
`__gap__:<state>`, not `__gap__:<state>:<symbol>`), same as the other three —
fine while Matrix only covers SPX/NDX, worth revisiting if a third symbol
is added.

**2026-09-17 incident**: `pick_selected` had no floor because the theory was
"picks don't change that often" — true for a healthy pick, false for a
flapping one. EdgeLane's `_pick_key` (`matrix_signals.py`) keys a pick's
identity on its legs/strikes, deliberately excluding composite score so a
score drift alone isn't a "new pick." But when SPX sat in a losing/`BROKEN`
structure with bias diverged, the engine kept re-striking a Bear Put every
poll — different legs each time, so each one *did* legally count as a new
pick under `_pick_key`, firing `pick_selected` repeatedly with only the
composite score changing (61.3, then 59.0) and the same "edge assumption
didn't hold up / Bias re-syncing" copy both times.

**Policy decision (2026-09-17): `pick_selected` is not a signal feed.** These
posts exist to show the tool is sharp, not to broadcast every trade idea —
so a couple a day is the target, and a BROKEN/diverged pick shouldn't post
*at all*, not even once per divergence episode. `MATRIX_MIN_GAP_HOURS_PICK_SELECTED="8"`
is the local backstop (this repo); the real fix belongs in
`matrix_signals.py::on_snapshot()`'s `# 1. pick_selected` block (EdgeLane
repo), which currently gates only on `_pick_key` + dwell. It should instead
skip firing entirely while the pick's health/verdict is `BROKEN`/`DO NOT
TRADE` or `state.last_trust_state[sym]` isn't `"in_sync"`, and only announce
once the pick is genuinely healthy — ideally reusing `win_rate_notable`'s own
earned-recovery gate (`recovered`/`crossed_green`, real graded wins behind
it) rather than firing the moment bias merely re-aligns.

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

## Delivery & error handling — no retries, ever

`bin/matrix_poster.py`'s push handler **always acks (204)**, whatever
`process_event` returns — posted, skipped, duplicate, or a genuine posting
failure. It never returns 500 to make Pub/Sub retry. Same rationale as
Simmer's (both were fixed together after the same 2026-09-16 incident): a
provider-side rejection like X's `402 "credits depleted"` isn't fixed by
retrying 10s later, and the old 500-on-error behavior turned one real
failure into 87 duplicate `ERROR` posts via automatic redelivery. **Log
once, don't retry** — a fix means a human re-firing the event, not a loop.

A dead-letter topic (`<sub>-dlq`, `ops/matrix/deploy.sh::ensure_dlq`, wired
via `--pubsub-only`) is a backstop, not the primary defense — it only
catches the residual case the poster's own try/except can't: a crash severe
enough Cloud Run never returns any response at all. GCP's minimum
`max-delivery-attempts` is 5, so that's the floor, not a chosen retry count.

## GCP plan (dependencies, component by component)

**What's shared with Simmer, and what deliberately is not:** the two service
accounts and the GCP project's Pub/Sub *service* are shared (there's only one
of each per project). The **topic** and **every container** are Matrix's own
— this was an explicit correction to the first draft of this doc, which had
assumed Matrix would reuse Simmer's topic and images.

| Component | Simmer (live) | Matrix (live) | Reuse? |
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
| Tier config | `products/facades/simmer_tier.config` | `products/facades/matrix_tier.config` | live, `_TIER_FILE_BY_ID` registered |
| Postiz channels | LinkedIn Simmer page, X `@facades_simmer` — both live | LinkedIn "Matrix" page, X `@facades_matrix` — both live | connected, ids in `.env` |
| Deploy script | `ops/simmer/deploy.sh` | **`ops/matrix/deploy.sh`** — its own script (§Build prerequisites: forked, not a generalized shared script) | ❌ forked, not shared |
| Preflight | `ops/simmer/preflight.sh simmer` | `ops/matrix/preflight.sh matrix` (a copy, same checks re-pointed at Matrix's own topic/services) | fork alongside `deploy.sh`, same checks |

The real config is `products/facades/matrix_tier.config` — read it directly
rather than a copy here going stale; same shape as `simmer_tier.config` plus
`MATRIX_MIN_GAP_HOURS_{SESSION_OPEN,GRID_DIGEST,DAILY_RECAP}` (§Gating).

## Build prerequisites (what has to exist before this can go live)

**EdgeLane side — the engine/UI already exist; the Postiz-facing layer does
not.** Full spec: `EdgeLane/docs/matrix_events_update.md`. Summary:
All shipped 2026-09-13:

1. **EdgeLane**: `app/matrix_events.py` (publisher) + `app/matrix_signals.py`
   (transition detection for the 7 states) + a hook at the end of
   `evaluator.py::evaluate_pending` (no separate watcher, as this doc
   specified) + `app/routes/matrix.py` (`GET /matrix/state/{SYM}?block=`) +
   `app/matrix_snap.py` (`GET /matrix/snap/{SYM}?view=`). Full map:
   `EdgeLane/docs/matrix_events_update.md`'s "Status — shipped" table.
2. **This repo**: `bin/matrix_poster.py`, `src/lib/sources/matrix_source.py`
   (`MatrixAPI`), `compose_matrix()` in `recipes.py`, the `matrix_api` branch
   in `factory.py`/`imagery.py`, `ops/matrix/{poster,snap}/` + `deploy.sh` +
   `preflight.sh` (forked from Simmer's, per the reasoning above — not a
   generalization), `Makefile` `matrix-*` targets,
   `_TIER_FILE_BY_ID["matrix"]` registered.
3. **Postiz**: X `@facades_matrix` + LinkedIn "Matrix" connected under the
   shared "Facades" customer; `matrix-api-token` secret created and rotated
   to the real value EdgeLane issued; `make matrix-preflight` green.

Two lessons worth carrying into the next Facades product (Torque):
- **`_to_card()`'s first mapping guess was wrong** — the real API nests
  the strategy data one level deeper (`data.pick`, `data.grid.grid`) than
  guessed, and has no `tags` array (reconstructed from
  `health`/`liquidity`/`composite_verdict.label` instead). Don't trust a
  provisional mapping until it's been run against the live endpoint at
  least once.
- **A config file's inline `# comment` can't safely contain an apostrophe**
  with the parser's old `shlex.split()` call — hit this twice writing
  `matrix_tier.config`. Fixed at the root in
  `config_loader.py::_first_token()` (a `#`-aware `shlex.shlex` instead),
  so this is no longer a trap for Torque's config either.

None of this touches Simmer — its topic, its containers, its images are
untouched by any of the above.

## Open questions

- Same Postiz "Facades" customer as Simmer, or its own? Went with "same"
  (`POSTIZ_CUSTOMER_ID_MATRIX` reuses the value) — cheap to change later if
  wrong, so not blocking.
- Matrix currently covers **SPX and NDX only**. Nothing here assumes
  otherwise (the poster/recipe code is symbol-agnostic), but don't test with
  arbitrary tickers (AAPL/NVDA/etc. were used for early pipeline tests before
  this was known — they compose fine on the minimal-card fallback, but
  aren't real Matrix coverage and shouldn't be mistaken for it).
