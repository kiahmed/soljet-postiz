# Facades · Simmer — posting pipeline

Simmer (`simmer.facades.trade`, EdgeLane's credit-spread watchdog) is onboarded
as a **standalone, event-driven product** in the publisher. Each Facades product
gets its own config file, its own Cloud Run poster + snap service, and its own
Pub/Sub subscription — nothing is shared except the topic and the Postiz box.

```
Simmer engine ──publish──▶ Pub/Sub topic  facades.ticker-events
   (state change)              │  attributes: product, symbol, state, expiry, event_id
                               ▼
                 subscription  simmer-poster-sub   filter: attributes.product="simmer"
                               │  push (OIDC)
                               ▼
                       Cloud Run  simmer-poster   (bin/simmer_poster.py --serve)
                         │  pull full card / blocks     │  POST {symbol,expiry}
                         ▼  GET edge.facades.trade      ▼  Cloud Run  simmer-snap
                   read-only Simmer API            headless-Chromium crop of
                   /simmer/state/<SYM>?block=…     [data-snap="card"] on ?snap=1
                         │
                         ▼   Postiz public API  (draft|now)
                   LinkedIn "Simmer"  +  X @facades_simmer (once authorized)
                   Firestore dedupe guard  (symbol+day+state | event_id)
```

## Pieces

| Path | What |
|---|---|
| `products/facades/simmer_tier.config` | the product tier (file-per-product; registered in `src/lib/config_loader.py::_TIER_FILE_BY_ID`) |
| `products/facades/simmer_context.md` | voice / brand context |
| `src/lib/sources/simmer_source.py` | `simmer_api` source — on-demand ticker state / full card |
| `src/lib/recipes.py::compose_simmer` | deterministic post text (`watch_entered` / `ready`) |
| `src/lib/imagery.py::_simmer_snap` | fetches the board crop from `SIMMER_SNAP_URL` |
| `bin/simmer_poster.py` | the subscriber/publisher — `--serve` (Cloud Run), `--pull` (local), `--event` (test) |
| `ops/simmer/snap/` | `simmer-snap` Cloud Run service (Playwright) |
| `ops/simmer/poster/Dockerfile` | `simmer-poster` Cloud Run image |
| `ops/simmer/deploy.sh` | one-command GCP provisioning per product |
| `ops/simmer/dev/` | local e2e: Pub/Sub emulator + API/snap stubs + `run-e2e.sh` |

## Local e2e

```bash
ops/simmer/dev/run-e2e.sh          # emulator + stubs + real Postiz DRAFTs, then teardown
ops/simmer/dev/run-e2e.sh --keep   # leave everything up to poke at
```
It publishes `ready` (MSTR), `watch_entered` (TSLA) and a `product=matrix` event
(must be filtered out by the subscription), pulls them with the poster in
`--dry-run`, then posts one real **draft** to the Simmer LinkedIn channel and
proves the dedupe guard blocks the replay.

Manual single event, no emulator:
```bash
export SIMMER_API_BASE=http://localhost:8899 SIMMER_SNAP_URL=http://localhost:8898/snap
python ops/simmer/dev/stub_api.py &   python ops/simmer/dev/stub_snap.py &
python bin/simmer_poster.py --event '{"product":"simmer","symbol":"MSTR","state":"ready","expiry":"2026-09-19"}' --dry-run
```

Against the **real** topic + a live EdgeLane (no stubs) — see
`docs/simmer.md` › "Validate against REAL EdgeLane events":
```bash
make simmer-sub-local
make -C ../../EdgeLane simmer-fire-event STATE=READY
make simmer-poster MODE=draft SUB=simmer-poster-sub-local
```

## Go live (GCP)

1. **EdgeLane** — engine publishes state-change events to `facades.ticker-events`;
   add `GET /simmer/ready` and `GET /simmer/state/<SYM>?block=card|score|gates|sentiment|evolution`
   (service-token auth); add `?snap=1` render mode to `simmer/ui`
   (`[data-snap="card"]` wrapper, nav hidden, per-symbol og:image + click-through).
2. **Secrets** — create `postiz-api-key` and `simmer-api-token` in Secret Manager.
3. **Identity** (shared by all Facades products — Simmer/Matrix/Torque):
   - `market-agent-sa` (`$GCP_SA_EMAIL`, the owner) *provisions* everything.
   - `facades-poster-sa` is the *runtime* SA — every `<product>-poster` /
     `<product>-snap` service and the Pub/Sub push auth run as it. Roles
     (resource-scoped, robotics-style): `pubsub.subscriber` per subscription,
     `run.invoker` per service, `secretmanager.secretAccessor` on the two
     secrets, `datastore.user` (project — Firestore has no collection IAM).
   `ops/simmer/deploy.sh simmer --sa-only` creates the topic + SA + all bindings
   (idempotent).
4. `ops/simmer/deploy.sh simmer` — full run: topic, SA, `simmer-snap`,
   `simmer-poster`, `simmer-poster-sub` (filtered push). `SIMMER_SNAP_URL` is
   wired from the deployed `simmer-snap` URL automatically; re-run
   `--poster-only` if snap is deployed after the poster.
5. **Postiz** — connect X `@facades_simmer`, create Customer "Simmer", fill
   `POSTIZ_INTEGRATION_ID_X_SIMMER` / `POSTIZ_CUSTOMER_ID_SIMMER` in `.env`.
6. Verify: `python bin/daily.py --check` and `python bin/social-status.py --tier simmer`
   list Simmer per channel; publish a test event; confirm the post + the dedupe row.

Matrix / Torque reuse the topic **and** `facades-poster-sa` — `deploy.sh <name>`
adds only that product's `run.invoker` + `secretAccessor` (on `<name>-api-token`)
bindings.

Matrix / Torque later: add `products/facades/<name>_tier.config`, a line in
`_TIER_FILE_BY_ID`, and `ops/simmer/deploy.sh <name>`.
