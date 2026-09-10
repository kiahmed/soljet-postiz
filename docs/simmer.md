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
3. **`?snap=1` render mode** in `simmer/ui` — a `[data-snap="card"]` wrapper
   around the board crop, nav/toasts hidden, per-symbol `og:image` +
   click-through to `/?symbol=<SYM>`. `simmer-snap` (deployed from here)
   screenshots it.

Until 1–3 exist, the pipeline below deploys cleanly but has nothing to consume.

## Idempotent provisioning (both sides)

The topic and `facades-poster-sa` are **shared** and may be created by either
repo. Provisioning from **either** side must be safe to re-run:

- `ops/simmer/deploy.sh` already is — `create … || (exists)` for the topic/SA,
  `create … || update …` for the subscription, and `add-iam-policy-binding` is
  idempotent by nature. Re-running it never recreates or errors on an existing
  resource.
- The EdgeLane side should do the same: if `facades.ticker-events` (or the SA)
  already exists, **ensure the role binding and move on** — don't recreate.
  Whichever repo runs first creates the resource; the other just binds to it.

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

`simmer-poster-sub` carries the filter `attributes.product="simmer"` — that is the
per-product isolation. Matrix/Torque each get their own `<name>-poster-sub` on the
**same topic**, reusing `facades-poster-sa`; an event for one product is never
delivered to another's poster.

## Adding Matrix / Torque

1. `products/facades/<name>_tier.config` + `<name>_context.md`
   (copy `simmer_tier.config`; change `TIER_ID`, channel/customer env vars,
   `PARENT_URL_TEMPLATE`, `POSTING_PURPOSE`, `POST_ON_STATES`).
2. One line in `src/lib/config_loader.py::_TIER_FILE_BY_ID`.
3. Create the `<name>-api-token` secret; `ops/simmer/deploy.sh <name>` — its own
   `<name>-snap`, `<name>-poster`, `<name>-poster-sub`. Reuses the topic and
   `facades-poster-sa` (adds only that product's `run.invoker` + `secretAccessor`).
4. Connect its Postiz channels; add `*_<NAME>` ids to `.env`.

Nothing about `simmer` — or the shared topic/SA — needs to change to add another
product.
