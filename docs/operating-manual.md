# Operating manual — set up, post, and automate a SolutionJet product

**Living document.** This is the canonical guide for adding a product to the
publisher and operating it — by hand from a laptop and on autopilot in the
cloud. Keep it current: when a step changes, edit it here in the same PR.

Related docs (don't duplicate — cross-reference):
- **[daily-posting.md](daily-posting.md)** — the day-to-day human routine, where
  hand-post images/text live, troubleshooting.
- **[linkedin-mentions.md](linkedin-mentions.md)** — how @mentions become real
  LinkedIn tags (slug → org URN) and the collision guard.
- **catalyst-knowledge-graph `docs/handle-resolution-spec.md`** — the upstream
  contract that supplies verified handles.

---

## 1. Mental model (read once)

- **One repo, many products.** All the logic — composition, entity selection,
  handles/URN mentions, imagery, scheduling — is **shared code** in `src/lib/`.
  A product adds **config, not code**.
- **Tiers.** A product is a *tier*. Tiers form a parent → branch tree:
  - **Parent tier** (`arboryx`) — the product itself; owns the channels.
  - **Branch tiers** (`arboryx.robotics`) — sub-modules that go deep on one
    sector; inherit the parent's channels and can add their own data source.
  - IDs are dotted: `<product>` and `<product>.<branch>`.
- **Selection is automatic and global.** `composer.primary_entities()` ranks a
  card's entities by relationship `confidence × impact_magnitude` (actor side
  ×1.25) to choose which entities get hashtags/@mentions. It's the same for every
  product — you don't configure it per product; the KG supplies the relationships.
- **Secrets & Postiz IDs never live in configs.** `tier.config` references them
  as `${VAR}`; the real values sit in `.env` (gitignored).

---

## 2. Register a new product tier for SolutionJet

Adding a sibling product to `arboryx` (e.g. a product called `acme`). Five steps.

### 2.1 Create the Postiz side (UI, once)
1. Connect the product's social channels in Postiz (X, LinkedIn, …).
2. Create a **Customer** in Postiz for the product (segmentation is by Customer,
   not Sets).
3. Note the **integration IDs** (one per channel) and the **Customer ID** — you
   need them for `.env` in 2.4.

### 2.2 Create the tier config
```
products/acme.ai/
  tier.config          # the parent tier
  context.md           # voice/brand context the composer reads
  branches/            # optional sub-modules
    <branch>/tier.config
    <branch>/context.md
```
Copy `products/arboryx.ai/tier.config` as a starting point and edit (field
reference in §3). At minimum set: `TIER_ID`, `TIER_NAME`, a `DATA_SOURCE_1_*`,
the channel IDs, `POSTIZ_CUSTOMER_ID`, and `POSTING_PURPOSE`.

### 2.3 Register the tier id (the ONE code touch)
`src/lib/config_loader.py` → add to `_TIER_DIR_BY_ID`:
```python
_TIER_DIR_BY_ID = {
    "arboryx": PRODUCTS_ROOT / "arboryx.ai",
    "arboryx.robotics": PRODUCTS_ROOT / "arboryx.ai" / "branches" / "robotics",
    "acme": PRODUCTS_ROOT / "acme.ai",                                  # ← new
    "acme.<branch>": PRODUCTS_ROOT / "acme.ai" / "branches" / "<branch>",  # if any
}
```

### 2.4 Add the secrets/IDs to `.env`
Configs expect these `${VAR}` names (mirror the arboryx pattern):
```
POSTIZ_INTEGRATION_ID_X_ACME=<x integration id>
POSTIZ_INTEGRATION_ID_LINKEDIN_ACME=<linkedin integration id>
POSTIZ_CUSTOMER_ID_ACME=<postiz customer id>
# shared, already present: HANDLE_ENDPOINT_URL, GOOGLE_APPLICATION_CREDENTIALS,
#   LINKEDIN_CLIENT_ID/SECRET
```
Then reference them in `tier.config` as `${POSTIZ_INTEGRATION_ID_X_ACME}` etc.

### 2.5 Verify it loads
```bash
make check                                  # shows every registered tier + its channels
make post-preview TIER=acme                 # compose without publishing
```
`make check` listing `acme` with its channels = registered correctly. If a tier
has no live channels it's treated as disabled (skipped, not an error).

> **Branch vs. parent:** a branch inherits channels via
> `CHANNELS_INHERIT_FROM="<parent>"` and can point `DATA_SOURCE_1` at its own KG
> collection. A card id that resolves in both parent and branch posts from the
> **branch only** (richer card + image).

---

## 3. tier.config field reference

Grouped; see a live file for exact syntax. Blank/absent = inherit parent (for
branches) or use the default.

**Identity**
| Field | Meaning |
|---|---|
| `TIER_ID` | dotted id, must match `_TIER_DIR_BY_ID` |
| `TIER_NAME` | human label |
| `TIER_PARENT` | parent id (`""` for a product, `<product>` for a branch) |
| `CONTEXT_FILE` | voice/brand file the composer reads (`context.md`) |
| `POSTING_PURPOSE` | free text steering tone/audience |

**Data source(s)** — `DATA_SOURCE_<n>_*`, source `1` is the daily feed
| Type | Use |
|---|---|
| `firestore` | parent findings collection (e.g. `findings`) |
| `firestore_cards` | KG per-card collection (`CKG-<Sector>/catalysts/items`) — the live cards the site serves |
| `duckdb` | local KG DuckDB (`_PATH`) |
| `firestore_inherited` | reuse the parent's Firestore, filtered (`_INHERIT_FROM`, `_FILTER_CATEGORY`) |

Common keys: `_GCP_PROJECT`, `_COLLECTION`, `_AUTH="gcloud_adc"`, `_PATH`.

**Channels**
| Field | Meaning |
|---|---|
| `CHANNEL_X_PRIMARY` / `CHANNEL_LINKEDIN` | `${integration-id}` per channel (parent) |
| `CHANNELS_INHERIT_FROM` | branch: inherit the parent's channels |
| `POSTIZ_CUSTOMER_ID` | `${customer-id}` — Postiz segmentation |

**Links & imagery**
| Field | Meaning |
|---|---|
| `KG_CARD_URL_TEMPLATE` | `https://<sub>/card/{card_id}` — deep link + og:image source |
| `PARENT_URL_TEMPLATE` | parent entry link (`?sector=&entry=&date=`) |
| `PUBLISH_SUBDOMAIN` | the product's site host |
| `IMAGERY_POLICY_X` | `link_card` (platform renders the card) |
| `IMAGERY_POLICY_LINKEDIN` | `attach` (download og image + attach) |
| `LET_PLATFORM_RENDER_LINK_CARD` | parent: let X render the link card |

**Tagging** (see linkedin-mentions.md)
| Field | Meaning |
|---|---|
| `HANDLE_INJECTION` | `true` = @-mention subject entities |
| `HANDLE_ENDPOINT_URL` | `${…}` resolver endpoint (or embed handles on cards) |
| `ENTITY_TAG_MODE` | `prefer_handle` \| `handle_only` \| `cashtag_only` \| `both` |
| `MAX_ENTITY_TAGS` | cap (default 2) |
| `CASHTAGS_ENABLED` | `$TICKER` cashtags (needs a US-listed marker; off by default) |
| `POST_TEXT_SOURCE` | `share` (KG's authored copy) \| `headline` |

**Branch toggles** (parent) — `BRANCH_<NAME>_ENABLED="true|false"`.

---

## 4. Post manually from a local machine

All posting runs through `bin/daily.py` (queue) or `bin/post.py` (one card),
wrapped by make targets. **Preview is the default; nothing publishes without
`--push` / `make post`.**

**Knobs (combine freely):** `OLDEST=1` oldest-unposted instead of newest ·
`CHANNEL=linkedin|x` one channel · `TIER=acme.<branch>` one tier.

```bash
# preview the next post for every enabled tier, all channels
make post-preview

# preview a specific tier / channel / oldest-first backlog walk
make post-preview TIER=acme OLDEST=1
make post-preview CHANNEL=linkedin

# actually publish (same knobs)
make post
make post OLDEST=1 TIER=acme.robotics CHANNEL=linkedin

# re-compose from scratch (discard the staged content_cache) then preview
make regenerate TIER=acme
```

**One specific card** (auto-detects the owning tier from the id; branch wins):
```bash
python3 bin/post.py --recipe single --source-id ROB-031226-008
python3 bin/post.py --recipe single --source-id ROB-031226-008 --channel linkedin
python3 bin/post.py --recipe single --source-id ROB-031226-008 --push
```

**Handle → URN cache** (LinkedIn real mentions) — after the KG fixes a slug,
clear the stale row so it re-resolves:
```bash
make social-cache-list  linkedin                 # inspect
make social-cache-clean  linkedin galaxea ai     # drop → re-resolves next post
make social-cache-update linkedin spirit ai      # drop + re-fetch now
```

**When X errors** (no API credit), the run catches it and routes the post to the
manual queue — it never crashes the job:
```bash
make manual-queue        # shows text + image to post by hand
```
See daily-posting.md §"Posting one by hand" for the `--copy`/`--show` helpers.

---

## 5. Run on autopilot (local cron or GCP prod)

The daily poster runs on one of two backends, chosen by `GCP_PROD_SCHEDULER` in
`.env` (`disabled` = local supercronic container, `enabled` = GCP Cloud
Scheduler → trigger sidecar). The same targets drive either backend:

```bash
make scheduler-up          # start (local container, or GCP jobs + trigger)
make scheduler-run         # fire ONE run now (test) — same as a real daily fire
make scheduler-logs        # follow it
make scheduler-restart     # re-apply the schedule after editing channels.conf
make scheduler-down        # stop + remove (local container, or GCP jobs)
```

### 5.1 Frequency & volume — `ops/scheduler/channels.conf`

Cadence is **config, not setup arguments**. One file holds it all — one row per
channel (`channel | count | delay | tier | cron`):

```
linkedin | 5 | 60m | arboryx.robotics | 0 6 * * *
x        | 5 | 70m | arboryx.robotics | 30 8 * * *
```

- `count` — cards posted per daily fire (X bills ~$0.20/post: cost dial).
- `delay` — spacing between cards within a run (`90` secs, or `60m`/`2h`).
- `cron`  — when the run fires (5-field; TZ = `SCHEDULER_TZ` locally,
  `GCP_SCHEDULER_TZ` on GCP).

The values are *baked into the backend at apply time*, so **edits only take
effect after re-applying**:
- **local** → `make scheduler-restart` regenerates the supercronic crontab
  (`ops/scheduler/crontab` is a generated, gitignored artifact — never edit it).
- **GCP** → `make scheduler-up` (or `-restart`) re-upserts the Cloud Scheduler
  jobs: the row's cron becomes the job's schedule, and count/delay/tier ride in
  the job's JSON payload to the trigger.

A row for a disabled channel is harmless (daily.py finds no integration and
no-ops), so you can pre-list a channel and flip its `*_ENABLED` flag later.

### 5.2 Prod setup on GCP (once)

Cloud Scheduler is only the **timer** — the posting logic stays on the machine
running the stack (it needs docker-exec confirmation, the sqlite posted-log, KG
card PNGs). Each job POSTs the trigger sidecar through the Cloudflare tunnel.

1. In `.env` set `GCP_PROD_SCHEDULER=enabled`, plus `GCP_SCHEDULER_REGION`,
   `GCP_SCHEDULER_TZ`, `SCHEDULER_TRIGGER_URL`, `SCHEDULER_TRIGGER_TOKEN`
   (long random string), `SCHEDULER_TRIGGER_PORT` (see `.env.example`).
2. `gcloud auth login` + `gcloud config set project <project>` on the machine
   you run make from (jobs are created via gcloud; the trigger itself needs no
   gcloud).
3. Cloudflare dashboard → the tunnel → add an ingress rule routing the
   `SCHEDULER_TRIGGER_URL` hostname (e.g. `trigger.arboryx.ai`) →
   `http://postiz-scheduler-trigger:8090`.
4. `make scheduler-up` — starts the trigger sidecar and creates one Cloud
   Scheduler job per channels.conf row (`postiz-daily-linkedin`, `postiz-daily-x`).
5. Verify: `make scheduler-run` fires every job now; `make scheduler-logs`
   shows Cloud Scheduler's view; the run itself logs to `data/daily.log`.

Dry-run without spending: `DRY=1 ./gcp-scheduler.sh create` prints the exact
gcloud calls; `SCHEDULER_DRY_RUN=1` makes a fired run echo its resolved
`make post` line instead of posting. Full design: `docs/scheduler.md`.

**What the scheduler needs** (already wired in `docker-compose.yaml`, confirm
per environment):
- `GOOGLE_APPLICATION_CREDENTIALS` — a service-account key with Datastore read,
  identity-mounted into the container (user ADC can't run headless).
- `HANDLE_ENDPOINT_URL: http://host.docker.internal:8084` — the handle resolver,
  reachable from inside the container (host uses `localhost:8084`).
- The KG repo mounted read-only if a tier reads `duckdb`/`cards` from it.

**A cloud fire does exactly what `make post` does locally** — one post per
enabled tier, per channel, published + confirmed; X failures → manual queue;
LinkedIn attaches the card image and resolves real @mentions.

> **Notifications:** there's no external notifier by design. Postiz's own UI
> surfaces failures, and the manual queue captures anything X couldn't publish.
> You don't need a laptop running.

---

## 6. Verify & heal

```bash
make check         # poller health + each tier's channels (the poster's own view)
make heal-check    # Temporal↔worker health only, exit 1 if unhealthy
make heal          # re-register broken pollers (fixes "stuck in QUEUE" posts)
make status        # docker + app/DB/Temporal health
```
If posts publish to Postiz but never leave `QUEUE`, the Temporal worker pollers
died — `make heal`. This is the historical #1 failure mode.

---

## 7. New-product checklist (copy per product)

- [ ] Postiz: channels connected, Customer created, IDs noted
- [ ] `products/<name>.ai/tier.config` (+ `context.md`, optional `branches/`)
- [ ] `_TIER_DIR_BY_ID` entry in `src/lib/config_loader.py`
- [ ] `.env`: integration IDs + customer ID
- [ ] `make check` lists the tier with its channels
- [ ] `make post-preview TIER=<name>` composes cleanly (text, image, tags)
- [ ] one real `make post TIER=<name>` verified in Postiz
- [ ] scheduler picks it up (enabled tier, live channels) — `make scheduler-run`

---

## Living document — keep this current
Update this file in the same PR whenever you: add a tier field, change a make
target, alter the scheduler wiring, or onboard a product with a new wrinkle. If
a step here is wrong, fixing the doc IS part of the task.
