# Simmer (Facades) — event-driven posting

Simmer is the first **event-driven** product in the publisher. It does **not**
use the daily scheduler (`ops/scheduler/`, `channels.conf`, the trigger sidecar) —
those stay arboryx-only. Instead the Simmer engine publishes ticker state-change
events to Pub/Sub and a dedicated Cloud Run service (`simmer-poster`) posts them.

Full picture + go-live: **`ops/simmer/README.md`**. This file is the operator's
quick reference.

## Model

| | Simmer | arboryx (for contrast) |
|---|---|---|
| Trigger | Pub/Sub event on ticker state change | Cloud Scheduler cron |
| Poster | `simmer-poster` Cloud Run (`bin/simmer_poster.py --serve`) | trigger sidecar on the box → `bin/daily.py` |
| Runs on | terminates on Cloud Run — nothing comes back to the box | this machine (docker-exec, sqlite, KG PNGs) |
| Data | EdgeLane read-only API over HTTP | KG Firestore / cards.json |
| Image | `simmer-snap` Cloud Run (headless-Chromium board crop) | KG-rendered PNG in GCS |
| Dedupe | Firestore `simmer_poster_dedupe` (`event_id` \| `symbol+day+state`) | `data/posted_log.sqlite` |
| Config | `products/facades/simmer_tier.config` (file-per-product) | `products/arboryx.ai/tier.config` (dir-per-tier) |

## Post moments (`POST_ON_STATES` in the tier config)

- `watch_entered` — "started simmering". Early, watchful, no trade call.
- `ready` — "ready to serve". Concrete: IV pct, VRP, expected-move band, expiry,
  news-sentiment read.

Text is **deterministic** (`src/lib/recipes.py::compose_simmer`) — the post must
equal the engine snapshot at publish time; no LLM rewrite.

## Market hours

`bin/simmer_poster.py` gates every event on the regular US session (9:30–16:00
America/New_York, Mon–Fri — `src/lib/market_hours.py`, no holiday calendar
yet). A `ready`/`watch_entered` event outside that window is skipped by
default: the read it's built from is a stale chain, not a live one.

**The one exception**: an event carrying the attribute `off_hours_catalyst=true`
still posts, with a disclaimer `compose_simmer()` appends automatically
("Alert generated while markets were closed…"). That flag is **EdgeLane's
call, never the poster's** — it has no news feed to judge a catalyst from.
See `EdgeLane/docs/simmer_off_hours_catalyst.md` for the (not-yet-built)
engine-side contract. `MARKET_HOURS_ENFORCED="false"` in the tier config
disables the gate entirely (testing only).

## Operate

```bash
# health / status — Simmer shows up per channel like every other product
python bin/daily.py --check
python bin/social-status.py --tier simmer
python bin/post-status.py --tier simmer

# compose a post for one ticker without publishing (uses the live/stub API)
python bin/post.py --recipe single --tier simmer --source-id SMR-MSTR-260919-260919   # (id form: SMR-<SYM>-<computed>-<expiry>, YYMMDD)

# process one event by hand (draft is the safe default; --mode now to publish)
make simmer-event EVENT='{"product":"simmer","symbol":"MSTR","state":"ready","expiry":"2026-09-19"}' DRY=1

# drain the subscription once from the box (catch-up / debugging)
make simmer-poster MODE=draft

# full local end-to-end (Pub/Sub emulator + stubs + real Postiz drafts)
make simmer-e2e
```

### Validate against REAL EdgeLane events (before the Cloud Run poster exists)

EdgeLane already publishes to `facades.ticker-events` (`SIMMER_EVENTS_ENABLED=true`
in `edgelane_market.config`). To confirm this side consumes them:

```bash
# 1. one-time: a PULL subscription on the real topic (refuses if the simmer
#    tier isn't enabled — registered + a live channel id in .env)
make simmer-sub-local                       # DRY=1 to print the gcloud calls

# 2. EdgeLane: fire a transition (from that repo)
make -C ../../EdgeLane simmer-fire-event STATE=READY     # or STATE=WATCH

# 3. drain it here → a DRAFT lands on the Simmer LinkedIn channel in Postiz
make simmer-poster MODE=draft SUB=simmer-poster-sub-local   # MODE=now to publish
```

Behaviour:
- **Gating** — `state` (`watch_entered`|`ready`) must be in `POST_ON_STATES`;
  `product` must be `simmer`; dedupe key is EdgeLane's deterministic `event_id`
  (`SMR-<SYM>-<YYMMDD>-<expiryYYMMDD>-<state>`).
- **Enrich is best-effort** — the poster re-pulls the ticker's card from
  `GET /simmer/state/<SYM>`. `make simmer-fire-event` fires a *synthetic*
  transition, so that endpoint 404s ("no readiness for <SYM>") and the post
  goes out **minimal, text-only** from the event attributes alone
  (`$SYM … / Expiry <date> / <link>`, logged `enrich_minimal`). A real
  engine transition carries full metrics and the board snapshot.
- **Empty subscription** — `--pull` is drain-once; a quiet sub surfaces a
  gRPC DeadlineExceeded which the poster treats as "nothing to pull" and
  exits 0.
- **Image** — a `simmer_api` post gets the `simmer-snap` crop or nothing;
  the KG-graph / LLM imagery ladder is never used. `SIMMER_SNAP_URL` empty =>
  text-only.

## What EdgeLane must provide (the upstream contract)

Three things live in the **EdgeLane repo**, not here:

1. **Event publisher** — the Simmer engine publishes a message to the topic
   `facades.ticker-events` on every ticker state change, attributes
   `product="simmer"`, `symbol`, `state` (`watch_entered`|`ready`|…), `expiry`,
   `event_id`. This is the only EdgeLane piece that touches GCP: it needs
   **`roles/pubsub.publisher` on that topic**. Reuse the EdgeLane backend's own
   service account (add the one binding) or `facades-poster-sa` — either works;
   it does **not** have to be the runtime SA.
2. **Read-only API** on `edge.facades.trade` — `GET /simmer/ready?since=` and
   `GET /simmer/state/<SYM>?block=card|score|gates|sentiment|evolution`,
   bearer-token auth (the token is in Secret Manager as `simmer-api-token`).
   Plain HTTPS — no GCP SA involved.
3. **`GET /simmer/snap/<SYM>`** — a dedicated, server-rendered standalone HTML
   card (`app/simmer_snap.py::render_snap_card`, inline CSS, no SPA), same
   bearer `simmer-api-token`. **Not** the live `simmer.facades.trade` SPA —
   that page sits behind a user-login session a headless browser doesn't
   have, so a screenshot of it only ever captures the sign-in dialog.
   `simmer-snap` (deployed from here) sets the bearer header, GETs this
   endpoint, and screenshots its `[data-snap="card"]` crop. See EdgeLane's
   `docs/simmer.md` › "Snapshot render endpoint (simmer-snap)" for the full
   contract (the SPA's old `?snap=1` mode still exists but is superseded for
   the poster by this endpoint).

Until 1–3 exist, the pipeline below deploys cleanly but has nothing to consume.

## Idempotent provisioning (both sides)

`facades-poster-sa` is **shared** across every Facades product and may be
created by any of their repos. `facades.ticker-events` is Simmer's own topic
(products after Simmer get their own topic each instead — see "Adding Matrix /
Torque" below). Provisioning from **either** side must be safe to re-run:

- `ops/simmer/deploy.sh` already is — `create … || (exists)` for the topic/SA,
  `create … || update …` for the subscription, and `add-iam-policy-binding` is
  idempotent by nature. Re-running it never recreates or errors on an existing
  resource.
- The EdgeLane side should do the same: if `facades.ticker-events` (or the SA)
  already exists, **ensure the role binding and move on** — don't recreate.
  Whichever repo runs first creates the resource; the other just binds to it.

## Delivery & error handling — no retries, ever

`bin/simmer_poster.py`'s push handler **always acks (204)**, whatever
`process_event` returns — posted, skipped, duplicate, or a genuine posting
failure. It never returns 500 to make Pub/Sub retry. This was a deliberate
change after a real incident (2026-09-16): X's media-upload API started
returning `402 "credits depleted"` for the Matrix X channel, and because the
poster used to 500 on any error, Pub/Sub redelivered the same failing event
indefinitely — 87 duplicate `ERROR` posts piled up in Postiz before it was
noticed. A provider-side rejection like that isn't fixed by retrying 10s
later; it just multiplies the mess. Now: **log once, don't retry** — a
genuine failure needs a human to fix the underlying cause (top up a quota,
patch a bug) and re-fire the event by hand, not an automatic loop.

A dead-letter topic (`<sub>-dlq`, `ops/simmer/deploy.sh::ensure_dlq`, wired
via `--pubsub-only`) is a backstop, not the primary defense — it only
catches the residual case the poster's own try/except can't: a crash severe
enough Cloud Run never returns any response at all. GCP's minimum
`max-delivery-attempts` is 5, so that's the floor, not a chosen retry count.

## Deploy / update on GCP

```bash
make simmer-preflight                    # check every GCP + .env dependency: green OK / red FAIL / yellow advisory
                                        #   (ops/simmer/preflight.sh <product> for matrix/torque)
make simmer-deploy DRY=1                 # print every gcloud command
make simmer-deploy PART=--sa-only        # shared: topic facades.ticker-events + facades-poster-sa + IAM
make simmer-deploy                       # + simmer-snap, simmer-poster, simmer-poster-sub (filtered push)
make simmer-deploy PART=--snap-only      # just re-deploy the screenshot service
make simmer-deploy PART=--sub-local      # a PULL sub on the real topic for local validation
```

Everything except `--sa-only` first checks the **tier is enabled** — registered
in `_TIER_FILE_BY_ID` *and* with a live channel id in `.env` (refuses otherwise).
You don't subscribe to the topic for a product that can't post.

**Identity** (shared by every Facades product):
- `market-agent-sa` (`$GCP_SA_EMAIL`) — the owner; runs `deploy.sh` / provisions.
- `facades-poster-sa` — the runtime SA all `*-poster` / `*-snap` services and the
  Pub/Sub push auth run as. Resource-scoped roles (robotics-style):
  `pubsub.subscriber` per sub, `run.invoker` per service, `secretAccessor` on
  `postiz-api-key` + `<product>-api-token`, `datastore.user` (project).

`simmer-poster-sub` carries the filter `attributes.product="simmer"` — that was
Simmer's per-product isolation while the plan was still "one shared topic."
As of Matrix, the standard changed: **each product after Simmer gets its own
topic and its own poster/snap containers**, reusing only the two service
accounts and the project's Pub/Sub service — see below.

## Adding Matrix / Torque

Matrix's own spec — its event/screenshot design, its own topic, and why its
deploy script is a **fork**, not a reuse of `ops/simmer/deploy.sh` — lives in
**[matrix_integration.md](matrix_integration.md)**. The steps below are the
generic shape; that doc is the concrete plan, and Torque should follow the
same fork-per-product pattern Matrix does.

1. `products/facades/<name>_tier.config` + `<name>_context.md`
   (copy `simmer_tier.config`; change `TIER_ID`, channel/customer env vars,
   `PARENT_URL_TEMPLATE`, `POSTING_PURPOSE`, `POST_ON_STATES`).
2. One line in `src/lib/config_loader.py::_TIER_FILE_BY_ID`.
3. Its own Pub/Sub topic (`facades.<name>-events`), its own
   `ops/<name>/{poster,snap}/` containers and `ops/<name>/deploy.sh` (forked
   from `ops/simmer/deploy.sh`, not a flag on it — see `matrix_integration.md`
   §Build prerequisites for why). Create the `<name>-api-token` secret;
   `ops/<name>/deploy.sh <name>` stands up `<name>-snap`, `<name>-poster`,
   `<name>-poster-sub`. Reuses `facades-poster-sa` and `market-agent-sa` only.
4. Connect its Postiz channels; add `*_<NAME>` ids to `.env`.

Nothing about `simmer` — its topic, its SA bindings, its containers — needs to
change to add another product.
