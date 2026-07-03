# Postiz Workbench

Operational notes for the Postiz local + Azure deployments managed from this directory.

## Local

Running locally via docker-compose. Public HTTPS available via Cloudflare Tunnel at `https://dev.arboryx.ai` (same container stack; tunnel routes to `postiz:5000` inside the Docker network).

Only notable warning: `OPENAI_API_KEY not set` (expected — copilot/chat features won't work until you add it to `.env`).

| Service        | URL                                             | Notes                                          |
| -------------- | ----------------------------------------------- | ---------------------------------------------- |
| Postiz UI (LAN)| `http://localhost:4007`                         | Frontend + API (`/api`)                        |
| Postiz UI (WAN)| `https://dev.arboryx.ai`                        | HTTPS via Cloudflare Tunnel — use for X OAuth  |
| Temporal UI    | `http://localhost:8080`                         | Workflow inspection (not exposed through tunnel)|
| Postgres       | container `postiz-postgres` (internal :5432)    | App database                                   |
| Redis          | container `postiz-redis` (internal :6379)       | App cache                                      |
| Cloudflared    | container `cloudflared` (outbound-only)         | Tunnel to Cloudflare edge                      |

### Cloudflare Tunnel

- Tunnel name: `arboryx-postiz-dev`
- Tunnel UUID: `<CF_TUNNEL_UUID>`
- DNS CNAME: `dev.arboryx.ai` → `<CF_TUNNEL_UUID>.cfargotunnel.com` (proxied)
- Ingress rule: `dev.arboryx.ai` → `http://postiz:5000` (inside Docker network)
- Access gate: **none** — the Access app "Arboryx Publisher" was deleted so the hostname is publicly reachable (required for X OAuth callbacks)
- Token lives in `.env` as `CF_TUNNEL_TOKEN`; the `cloudflared` service in `docker-compose.yaml` reads it at startup
- Postiz `MAIN_URL` / `FRONTEND_URL` / `NEXT_PUBLIC_BACKEND_URL` are set to `https://dev.arboryx.ai` so auth cookies and redirects work correctly

**To disable the tunnel** (revert to localhost-only): comment out the `cloudflared` service in `docker-compose.yaml`, revert the three URL vars in `.env` to `http://localhost:4007`, then `./update.sh`.

### Known issue + fix: backend startup race

On `./update.sh` (or any `docker compose up -d --force-recreate`), the Postiz backend (internal process on container port 3000) can hang silently during startup if Temporal (`temporal:7233`) isn't ready yet. Symptoms:
- UI loads (`/auth` returns 200) but every `/api/*` call returns 502 "no live upstreams"
- `docker exec postiz pm2 list` shows `backend: online` but `ss -tln` shows no listener on :3000
- pm2 backend stdout is empty beyond the launch command (no NestJS startup logs)

**Fix applied (2026-04-23):**
- `temporal` service now has a `healthcheck` (`tctl --address temporal:7233 cluster health | grep SERVING`) with 20s start period + 10s interval
- `postiz.depends_on` now waits for `temporal: service_healthy` (in addition to postgres/redis)
- `update.sh` now verifies backend is listening on :3000 after `up -d`, and auto-kicks `pm2 restart backend` inside the container if not (self-healing fallback)

If it ever surfaces again, the one-shot manual fix is:
```bash
docker exec postiz pm2 restart backend
```

Lifecycle scripts:

- `./deploy.sh` — pull images, start stack, wait for health
- `./status.sh` — `docker compose ps` + health checks
- `./update.sh` — pull latest images, recreate containers, auto-prune dangling images left behind
- `./teardown.sh` — stop containers (volumes preserved)
- `./cleanup.sh` — reclaim Docker disk space (dangling images + build cache). Flags: `--stopped` also removes stopped containers; `--deep` prompts to remove unused tagged images (may impact other projects). Safe by default — never touches volumes, never touches running-container images.

## Azure

Fully wired via an `AZURE_ENABLED` toggle in `.env`. **Nothing is provisioned yet.**

To deploy: set `AZURE_ENABLED=true` in `.env`, then run `./azure-deploy.sh`.

### Files

| File                  | Purpose                                                                                                       |
| --------------------- | ------------------------------------------------------------------------------------------------------------- |
| `.env` (Azure block)  | All Azure config — subscription, RG, region, VM size, SSH key path, DNS label, NSG ports                      |
| `cloud-init.yaml`     | VM bootstrap — installs Docker + compose plugin, bumps `vm.max_map_count` for Elasticsearch                   |
| `azure-deploy.sh`     | Idempotent provision: RG → VM → NSG rules → wait for SSH + cloud-init → scp app + rewritten `.env` → `deploy.sh` remotely |
| `azure-status.sh`     | RG/VM state, SSH check, remote `docker compose ps`, HTTP health                                              |
| `azure-update.sh`     | SSH + `docker compose pull && up -d --force-recreate`                                                        |
| `azure-teardown.sh`   | RG-name confirmation gate before deleting the resource group                                                 |

### Defaults baked into `.env`

- Subscription: `<your-azure-subscription-id>` (Pay-As-You-Go, taken from `az account show`)
- Tenant: `<your-azure-tenant-id>`
- Resource group: `rg-solutionjet-postiz` (created if missing)
- Region: `eastus`
- VM: `postiz-vm`, `Standard_B4ms` (4 vCPU / 16 GB), Ubuntu 24.04, 64 GB OS disk
- Admin user: `azureuser`
- SSH key: `~/.ssh/id_rsa.pub`
- DNS label: `arboryx-postiz` → `arboryx-postiz.eastus.cloudapp.azure.com`
- NSG ports: `22, 80, 443, 4007, 8080`
- Remote app dir: `/opt/postiz`

### Pre-flight checklist (before flipping `AZURE_ENABLED=true`)

1. Confirm an SSH key exists at `~/.ssh/id_rsa.pub` (or change `AZURE_SSH_KEY_PATH`).
   Generate one with: `ssh-keygen -t ed25519 -f ~/.ssh/id_rsa -N ''`
2. Tighten `AZURE_SSH_SOURCE_CIDR` to your IP (currently open to the world).
3. Decide if port 8080 (Temporal UI) should be public — currently in `AZURE_OPEN_PORTS`; remove for production.
4. Plan TLS — current setup is HTTP-only. Follow-up: add Caddy reverse proxy with Let's Encrypt on the same VM and front Postiz on 443.

### Deploy flow (what `azure-deploy.sh` does)

1. Validates `AZURE_ENABLED=true`, required vars, SSH key, `az`/`ssh`/`scp` tooling.
2. Verifies / switches to `AZURE_SUBSCRIPTION_ID`.
3. Creates resource group if missing.
4. Renders `cloud-init.yaml` (substitutes `{{ADMIN_USER}}`, `{{APP_DIR}}`).
5. Creates VM (reuses if present — cloud-init does NOT re-run on existing VMs).
6. Adds NSG rules for each port in `AZURE_OPEN_PORTS`.
7. Resolves public IP / FQDN.
8. Waits for SSH (~3 min cap) and cloud-init bootstrap marker (~5 min cap).
9. Builds a remote `.env` — rewrites `MAIN_URL`, `FRONTEND_URL`, `NEXT_PUBLIC_BACKEND_URL` to the public host.
10. `scp`s `docker-compose.yaml`, `dynamicconfig/`, all `*.sh`, and the rewritten `.env` to `/opt/postiz`.
11. Runs `./deploy.sh` remotely.

### Cost ballpark

`Standard_B4ms` VM + Standard public IP + 64 GB Premium SSD ≈ **$60–80/mo**. Drop to `Standard_B2ms` (2 vCPU / 8 GB) for ~$30/mo if memory headroom isn't needed.

## Brand hierarchy

```
SolutionJet (business → Postiz Organization)
  └── Arboryx.ai (active product; X handle @arboryx_ai)
        ├── Arbor (module under Arboryx.ai — not a separate product)
        └── catalyst-knowledge-graph (sibling repo at ../catalyst-knowledge-graph,
                                       module today, may become its own product later)
```

Renames on 2026-04-16: "Soljet" → **SolutionJet**, "AlphaSnap" → **Arboryx.ai**. Arbor demoted from top-level product to a module inside Arboryx.ai.

## Multi-business / multi-product organization

This Postiz instance serves SolutionJet's products and is designed to scale to additional businesses later without extra Postiz deployments.

### Postiz hierarchy

```
Organization (Postiz: "Organization" / "Workspace" in some versions)
  └── Team members (you, optionally invitees)
  └── Channels  (one per connected social account)
  └── Sets       (named groupings of channels you can post to as a bundle)
```

### Mapping

| Your concept          | Postiz concept    | Example                                             |
| --------------------- | ----------------- | --------------------------------------------------- |
| **Business / Entity** | **Organization**  | "SolutionJet" (today), future businesses later     |
| **Product**           | **Set** inside org | "Arboryx.ai" (set); future products each a new set |
| **Handle per platform** | **Channel**     | @arboryx_ai (X); LinkedIn/Reddit handles TBD       |

**Rule:** Business → Organization. Product → Set. Handle → Channel. Channels don't leak between orgs, so when Business B is added later it stays isolated. Within an org, Sets let you post to "all Arboryx.ai handles" in one click.

### X: no account-to-account delegation

- X has no parent-child ownership between accounts anymore. Old X Teams / TweetDeck delegation is deprecated / gated behind Premium.
- Don't route @ArborApp through a parent "Soljet Official" handle. Connect @ArborApp directly via OAuth.
- The real "business owns this handle" link lives in your password manager (recovery email, 2FA, billing under the business name).

### LinkedIn: real hierarchy via Company Pages

- Each product has its own **LinkedIn Company Page** (Arbor, AlphaSnap, etc.)
- Your personal LinkedIn is added as **Super Admin** on each page
- In Postiz, connect LinkedIn once — log in as yourself, then choose which Company Page to post as
- Repeat per page, each stored as a separate channel
- LinkedIn dev app needs `w_organization_social` scope for page posting

#### LinkedIn rollout status (2026-04-25) — blocked on Page creation

- **New Company Page** at `linkedin.com/company/setup/new` → 7-day account-trust restriction error
- **Showcase Page** from SolutionJet admin tools → also blocked (same 7-day rule applies to Showcase)
- API can't bypass — LinkedIn's Page-creation endpoint isn't public; Organization APIs only manage existing pages
- **Workaround plan while waiting:**
  1. Create dev app at `developer.linkedin.com/apps`, associate with SolutionJet Page during creation
  2. Request **Community Management API** access immediately (review takes days-to-weeks)
  3. OAuth redirect URI: `https://dev.arboryx.ai/integrations/social/linkedin` — no trailing slash
  4. Connect SolutionJet Page to Postiz as temporary Arboryx outlet; post Arboryx content under SolutionJet voice
  5. When window opens → create Arboryx Showcase Page from SolutionJet admin → connect as 2nd channel using same dev app keys (no rewiring)
- `.env` vars to add: `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`. Reload via `docker compose up -d --force-recreate postiz` (not `restart`)

### First-product wiring — DONE (2026-04-23)

- Org: **SolutionJet** (id `<SOLUTIONJET_ORG_ID>`, apiKey in DB)
- Customers (product segmentation): **Arboryx.ai** (`ab2d1389-…`), **Robotics** (`71433a31-…`)
- Connected channels:
  - @arboryx_ai (X, integration id `<POSTIZ_INTEGRATION_ID_X_ARBORYX>`) — assigned to Arboryx.ai customer
  - @SolutionjetInc (X, integration id `<POSTIZ_INTEGRATION_ID_X_SOLUTIONJET>`) — unassigned (business-level)
- LinkedIn/Reddit: TBD once dev apps are registered

**Correction:** Postiz `Sets` are saved post templates (with a `content` field), not channel groupings. Use **Customer** for per-product channel segmentation — that's what has the Integration FK.

### Product context lives in `products/arboryx.ai/`

Postiz has no knowledge-base feature. Context for draft generation lives in this repo:
- `products/arboryx.ai/context.md` — voice, metaphor (arbor/branches/leaves/buds/catalysts/fruits), data sources
- `products/arboryx.ai/handles.yaml` — platform handles + Postiz integration IDs
- Data sources read by the draft pipeline: `../arboryx.ai/` (product app) + `../catalyst-knowledge-graph/` (KG)

### Creating drafts via the Postiz API

Org API key (from `Organization.apiKey`): stored in `.env` as `POSTIZ_API_KEY` (gitignored).
Pull the live value from the `Organization.apiKey` column in the Postiz DB, or
generate one via Postiz UI → Settings → API. Do not commit it here.

Minimal valid draft payload for X:
```json
{
  "type": "draft",
  "shortLink": false,
  "date": "2026-04-24T12:00:00.000Z",
  "tags": [],
  "posts": [{
    "integration": { "id": "<POSTIZ_INTEGRATION_ID_X_ARBORYX>" },
    "value": [{ "content": "…", "image": [] }],
    "settings": {
      "__type": "x",
      "who_can_reply_post": "everyone",
      "active_thread_finisher": false
    }
  }]
}
```

Endpoints:
- `POST /api/public/v1/posts` — create (use `type: draft | schedule | now`)
- `GET  /api/public/v1/posts` — list
- `DELETE /api/public/v1/posts/{id}` — sometimes returns `{err:true}` for drafts; if so, fall back to SQL `DELETE FROM "Post" WHERE id = '...'`
- `POST /api/public/v1/upload` — upload media before attaching to a post
- `GET  /api/public/v1/integrations` — list channels for your org

### Auto-posting options in Postiz

| Flavor | Mechanism | Good for |
| --- | --- | --- |
| **Scheduled single post** | `type: "schedule"` with future date | One-off future post |
| **Recurring templates** | `Sets` table — save a post template, Postiz publishes on cadence | Evergreen loops |
| **Plugs (automation chains)** | Per-channel automation: "event → action → delay → action" | Cross-post, auto-reply, etc. |

### Draft-from-catalyst pipeline (v1 wired 2026-04-30)

Tier architecture, data-source layer, and CLI are in place. The composer is template-based for v1; a clean `_llm_rewrite()` hook in `src/lib/composer.py` is ready for an OpenAI/Anthropic pass when desired.

#### Tier system

Each "tier" is a content scope with its own context, data sources, and channels. Tier configs are shell-style key=value files (mirrors `arboryx-admin/arboryx_admin_backend.config` style, sourceable from bash, parsed by `src/lib/config_loader.py`).

```
products/arboryx.ai/
  tier.config                # parent tier (id: arboryx)
  context.md                 # parent voice + arbor metaphor + posting purpose
  handles.yaml
  branches/
    robotics/
      tier.config            # branch tier (id: arboryx.robotics)
      context.md             # robotics-specific domain context
```

Branches inherit parent context (composed in order: parent → branch) and parent channels (additive). Branches can declare new data sources or filter inherited ones (e.g., `firestore_inherited` + `filter_category="Robotics"`).

#### Data sources per tier

| Tier              | Source            | Auth | What                                                   |
| ----------------- | ----------------- | ---- | ------------------------------------------------------ |
| arboryx (parent)  | Firestore `findings` in `marketresearch-agents` | gcloud ADC | Six-sector daily findings with sentiment/analysis |
| arboryx.robotics  | `../catalyst-knowledge-graph/data/robotics.duckdb` | local file | Catalysts + entities + relationships |
| arboryx.robotics  | inherited Firestore, filtered to `category=Robotics` | gcloud ADC | Robotics findings only |

Note: `data/arbor.duckdb` in the KG repo is a stale duplicate — ignore it.

#### CLI

```bash
# List recent items across all sources for a tier
bin/list-recent.py --tier arboryx --since 1d
bin/list-recent.py --tier arboryx --since 1d --unposted    # hide already-posted
bin/list-recent.py --tier arboryx.robotics --since 7d

# Inspect KG schema (helpful first time against robotics.duckdb)
bin/list-recent.py --tier arboryx.robotics --schema

# Draft / schedule / publish for a specific source item (legacy single-source CLI)
bin/draft.py --tier arboryx --source-id <id>                          # print draft
bin/draft.py --tier arboryx --source-id <id> --push                   # → Postiz as draft

# Unified CLI — recipe-driven (preferred)
bin/post.py --recipe single --tier arboryx --source-id <id>           # one finding/catalyst → one post
bin/post.py --recipe narrative --tier arboryx --slug launch --push    # file-based (launch, about)
bin/post.py --recipe sector-digest --tier arboryx --sector Robotics --since 7d --push
bin/post.py --recipe event --tier arboryx --title "EU AI Act Phase 2 lands" \
            --body "What changes for embodied-AI deployments" --link https://… --push

# All recipes accept the common flags:
#   --push --mode draft|schedule|now [--at <iso> | --in 2h|30m|1d] [--force]
# bin/post.py uploads any media in the bundle and creates one Postiz post that
# fans out to every enabled channel in the tier (LinkedIn included only when
# LINKEDIN_ENABLED=true).

# Inspect what's been posted
bin/posted-log.py
bin/posted-log.py --tier arboryx.robotics
```

#### Recipes

| Recipe          | Input                                                            | Use it for                                          |
| --------------- | ---------------------------------------------------------------- | --------------------------------------------------- |
| `single`        | `--source-id <Firestore doc id or KG catalyst id>`               | The default — one catalyst per post.                |
| `narrative`     | `--slug <name>` reads `products/<tier>/narratives/<slug>.md`     | Launch posts, evergreen "about", screenshot-anchored intro posts. Frontmatter: `media`, `llm_rewrite` (default false — narrative copy is intentional). |
| `sector-digest` | `--sector <name> --since 7d`                                     | Weekly summary of N similar developments. LLM condenses bullets into one post. |
| `event`         | `--title --body --link [--media]`                                | Ad-hoc: regulatory shift, industry pivot, conference recap. LLM polishes by default; `--no-llm` for verbatim. |

Narratives shipped:
- `products/arboryx.ai/narratives/launch.md` — Arboryx.ai webapp launch (fill in `assets/arboryx-screenshot.png`).
- `products/arboryx.ai/narratives/about.md` — evergreen explainer.
- `products/arboryx.ai/branches/robotics/narratives/robotics-launch.md` — Robotics branch launch (fill in `assets/robotics-screenshot.png`).

Drop screenshots into `assets/` (gitignored if you don't want them tracked) and the `media:` line in the narrative frontmatter resolves them.

Each successful push fans out to ALL enabled channels in the tier (X primary + X business + LinkedIn when gated on) in a single `/api/public/v1/posts` call. The post is logged to `data/posted_log.sqlite` keyed on `(source_type, source_id, tier)`, so the same finding never gets re-drafted by accident.

Setup once:
```bash
pip install -r requirements.txt
gcloud auth application-default login           # for Firestore ADC
# POSTIZ_API_KEY is already populated in .env (Org apiKey from Postiz DB).
# OPENAI_API_KEY is already in .env — drives LLM rewrite automatically.
```

#### LLM rewrite (OpenAI)

`src/lib/composer.py:_llm_rewrite()` calls OpenAI (`gpt-4o-mini` by default, override via `OPENAI_MODEL`) with the composed parent+branch context.md + posting purpose as the system prompt. OpenAI's automatic prompt caching kicks in for prompts >1024 tokens, so the large context block is cached for free across repeat calls. Without `OPENAI_API_KEY` set, the composer falls back to the deterministic template — drafts still work, just less polished.

#### LinkedIn gate (built, currently off)

LinkedIn dev-app fields live in `.env`:
- `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` — fill once dev app exists
- `LINKEDIN_ENABLED=false` — runtime gate; flip to `true` when Postiz channel is connected
- `LINKEDIN_BACKFILL_DAYS=7` — used by future `bin/backfill-linkedin.py`

Tier configs hold `CHANNEL_LINKEDIN=""` placeholders. When you connect the LinkedIn channel in Postiz UI, populate `CHANNEL_LINKEDIN` with the integration ID. The dispatcher (`bin/_common.py:integration_ids_for`) skips it while `LINKEDIN_ENABLED=false`; flipping the gate fans out new drafts. Backfill of recent posts is a separate one-time job (TBD: `bin/backfill-linkedin.py`).

When Business B is added later: **Create Organization → "Business B"** and repeat the whole pattern inside it. Channels don't leak between orgs.

### Postiz `/posts` payload — required fields (learned 2026-05-01)

First end-to-end run of `bin/post.py --recipe narrative --tier arboryx --slug launch --push --mode draft` returned 400 — Postiz NestJS DTOs are stricter than the cookbook implied. Fixed in `src/lib/postiz_client.py` and `bin/post.py`:

- **`date`** is required for `type=draft` too (not just schedule/now). Defaults to `now()` ISO if not provided.
- **`tags`** must be an array (use `[]`).
- **`posts[].settings.who_can_reply_post`** must be one of `everyone | following | mentionedUsers | subscribers | verified`. We default to `everyone`.
- **`posts[].value[].image[]`** items must include both `id` AND `path` (the upload response shape: `{id, name, path, ...}`). Sending only `{id}` is rejected with "path should not be null".
- Image filenames must use a valid extension and **no spaces** — `postiz_client.upload()` now strips spaces from the sent filename.
- Banner-class assets (>5MB) sometimes return Cloudflare 502 on first try; bumped upload timeout to 180s. Resize to ~2MB max if it keeps failing.
- `POST /api/public/v1/posts` returns `[{postId, integration}, ...]` (one entry per integration), NOT `{id}`. Updated post-id extractor in `bin/post.py` accordingly.
- `GET /api/public/v1/posts` requires `startDate` + `endDate` ISO query params, plus `display=draft|all|...`. Results are filtered by the API key's customer scope — drafts created for an integration in a different customer are visible only to that customer's API key.

First Arboryx.ai launch draft now lives in Postiz: `postId=cmonm8d6p0000pv784x1l271u` (Arboryx.ai @arboryx_ai) + `cmonm8de80001pv78l2jql1ki` (SolutionJet @SolutionjetInc). State=DRAFT, image=`assets/banner.png`.

### Why `--mode now` left posts in `state=QUEUE` then `ERROR` (learned 2026-05-01)

Two stacked failures hit when running `bin/post.py --recipe narrative --tier arboryx --slug launch --push --mode now --force`:

**1. Orchestrator was crashed (Temporal worker missing).** Postiz's `orchestrator` PM2 process establishes a Temporal client at boot, then bundles a worker per provider on the `main` task queue. On the running container the orchestrator had crashed at boot 2026-04-30 with `Failed to create worker connection ... ConnectionRefused 172.18.0.2:7233` (Temporal wasn't ready yet). Without the worker, every post the backend wrote to `Post (state=QUEUE)` had no consumer and sat indefinitely. A simple `pm2 restart orchestrator` then crash-looped with `EADDRINUSE :::3002` because PM2 spawned a new orchestrator while the previous one still held the port. **`docker compose restart postiz` was the clean fix.**

**2. After the worker came up, the X dev app had 0 credits.** Direct probe with the integration's stored OAuth1 token (`Integration.token` = `accessToken:accessSecret`):
```
GET  https://api.x.com/2/users/me   → 200 (auth fine; returned arboryx_ai)
POST https://api.x.com/2/tweets     → 402 CreditsDepleted
   {"detail":"Your enrolled account [<X_DEV_APP_ACCOUNT_ID>] does not have any credits to fulfill this request."}
```
X moved tweet creation to a credit/PAYG model. Postiz's `XProvider.handleErrors()` (in `libraries/nestjs-libraries/src/integrations/social/x.provider.ts`) only matches a few error strings (`Unsupported Authentication`, `usage-capped`, `duplicate-rules`, invalid URL, video-too-long); `CreditsDepleted` falls through to the generic `BadBody('','{}',{})` — that's the `"Unknown Error" / type:"bad_body"` blob that ends up in `Post.error` and the `Errors` table. The real X response body is dropped on the floor. **To get past it: top up credits at developer.x.com for the underlying dev app (account_id `<X_DEV_APP_ACCOUNT_ID>`), or upgrade the app's API tier.**

**SolutionJet integration is soft-deleted.** `Integration.deletedAt` for `<POSTIZ_INTEGRATION_ID_X_SOLUTIONJET>` is `2026-04-24 02:38:20.644`. Posts to it currently bounce; reconnect in Postiz UI if it should remain a real channel, or remove `CHANNEL_X_BUSINESS` from `products/arboryx.ai/tier.config`.

**Banner image: resized for X.** The 6.5 MB / 3584×1184 PNG exceeds X's 5 MB image cap. Resized to 2400×792 progressive JPEG q=88 (≈0.33 MB) at `assets/banner-x.jpg` (Pillow). `products/arboryx.ai/narratives/launch.md` now points at the JPEG.

**Debug ladder when posts won't publish:** see `reference_postiz_api.md` memory for a copy-paste version (pm2 list → tctl taskqueue describe → workflow describe → SELECT FROM "Errors" → direct X API probe).

### Thread support (built 2026-05-01)

Free-tier X caps tweets at 280 chars. Drafts longer than that auto-split into a numbered thread, dispatched as Postiz's `posts[].value: [{...}, {...}]` shape (each entry = one sub-tweet, media on the first only).

- New: `src/lib/thread.py:split_for_thread(text, max_chars=280, marker_reserve=8)` — prefers paragraph > sentence > line > whitespace boundaries; falls back to a hard cut only when no whitespace exists in the budget. Appends ` 1/N` markers. Reserve covers ` 99/99` + small buffer for short URLs that X normalises to 23 chars.
- `PostBundle` now has an optional `parts: list[str] | None` — recipes can set explicit thread breaks; otherwise the dispatcher auto-splits `text`.
- `bin/post.py` calls the splitter, prints each part with its char count in dry-run, and passes `parts=` to `PostizClient.create_post`.
- `PostizClient.create_post` now takes `parts=[...]` (preferred) or `text=` (single-tweet wrapper). Backwards-compatible.

Smoke results: launch narrative (251 chars) returns `[text]` unchanged. A 743-char synthetic sector-digest splits cleanly into 4 parts of 223/156/165/212 chars at sentence boundaries.

### Auto-imagery (built 2026-05-01)

Image selection is automatic — `bin/post.py` calls `src/lib/imagery.auto_media(tier, bundle, recipe_name)` after composition, no flag needed. Caches outputs under `data/imagery_cache/` so repeat calls are free. Disable per-call with `--no-auto-media`; user-provided `media_paths` always wins.

**Strategy ladder (first hit wins):**
1. **Explicit media** — frontmatter `media:` or `--media`. Untouched.
2. **KG card screenshot** — when the tier sets `KG_CARD_URL_TEMPLATE` (e.g. `https://robotics.arboryx.ai/cards/{source_id}`) and Playwright is installed. Captures the rendered card; best for catalyst-anchored singles. (Playwright not in `requirements.txt` yet; install with `pip install playwright && playwright install chromium` to enable.)
3. **External-URL skip** — if the post text contains a non-`arboryx.ai` URL, return `[]`. X auto-renders the link's og:image as a tweet card; our brand card would only fight that.
4. **Branded PIL card** — for `sector-digest`, `event`, or any post with an inferable sector. Renders 1200×675 JPEG: sector accent band, sector tag, headline (auto-wrapped), subtitle (wrapped), bullet headlines (digest), `arboryx.ai` footer. Sector palette is in `imagery.SECTOR_PALETTE`.
5. **DALL-E 3** — opt-in via `IMAGERY_GENERATE=true`. Prompt is LLM-derived from post text. Uses `OPENAI_IMAGE_MODEL` / `OPENAI_IMAGE_SIZE` env knobs.
6. **None** — better than a wrong image.

**Sector inference** uses three layers in order: explicit `ctx["sector"]` (set by recipes from Firestore `category` / DuckDB `sector`/`category` fields), regex on the post text (matches sector name, no-space form, or `#FirstWord` hashtag), and finally `_llm_infer_sector(text)` via gpt-4o-mini (~$0.0001/call) — default-on when `OPENAI_API_KEY` works, disable with `IMAGERY_LLM_INFER=false`. The LLM fallback is what catches "FAA Part-450 sub-orbital reusable launches" → Space & Defense without "space" appearing literally.

**Optional LLM strategy router:** `IMAGERY_LLM_ROUTER=true` runs the LLM ahead of the rules to bump priority. The router only reorders or skips strategies; it never bypasses explicit user media. Off by default.

**OpenAI quota dependency:** the LLM inferrer, router, and DALL-E all share `OPENAI_API_KEY`. If the key returns 429 / `insufficient_quota`, every LLM path silently falls back (regex sector inference, default palette, no DALL-E generation). The post still ships with a reasonable card, just less smartly routed. `IMAGERY_DEBUG=true` surfaces the underlying API errors on stderr.

### Unified LLM router (built 2026-05-01)

`src/lib/llm.py` is the single point of LLM access for the project:

- **`chat(system, user, max_tokens=256)`** — tries OpenAI (`OPENAI_MODEL`, default `gpt-4o-mini`) first; on **any** failure (network, quota, parse, missing key, missing package) silently falls back to Gemini (`GEMINI_TEXT_MODEL`, default `gemini-2.5-flash-lite`). Returns the response text or `None` if both providers fail. Set `LLM_DEBUG=true` to surface provider errors on stderr.
- **`generate_image(prompt, out_path)`** — Gemini-only via `GEMINI_IMAGE_MODEL` (default `gemini-3-pro-image-preview`). Validates the output (decodable, ≥5KB, ≥600×400 px); deletes the file and returns `False` if validation fails so we never serve a half-baked image. Returns `True` on success.

Both call sites in the codebase route through this:
- `src/lib/composer.py:_llm_rewrite` — voice-aligned draft rewrite
- `src/lib/imagery.py:_llm_pick_strategy`, `_llm_infer_sector`, `_generate_image`

**Why OpenAI primary, Gemini fallback for text:** OpenAI's gpt-4o-mini is the cheapest reliable option for short copy work. Gemini Flash Lite is comparable quality at a similar price tier and the user's account is paid, so it's the resilient backstop. As of 2026-05-01 the OpenAI key is in `insufficient_quota` (429) — *every* text call currently goes Gemini. The pipeline behaves identically; only the path differs.

**Why Gemini-only for image generation:** the user's OpenAI tier is exhausted; Gemini's image model returns higher-quality stills than the DALL-E tier we'd have access to anyway. `gemini-3-pro-image-preview` produced a clean, on-prompt editorial photo on first try for the test prompts (humanoid robot for a robotics post; abstract circuit-substrate macro for a generic finding) — both 700KB+, 1408×768, no text/logos artifacts.

**Validation guards on generated images** prevent a "burned credit, garbage image" outcome:
1. Response must contain `inline_data` — empty responses are caught before any disk write.
2. File must be ≥5,000 bytes (strips obvious placeholders).
3. PIL must decode it (via `Image.verify()`).
4. Dimensions must be ≥600×400 px (X minimums).

If any guard fails the file is deleted and `_generate_image` returns `None`. The strategy ladder then either falls through to the next option or returns `[]`.

**Image-gen ladder position:** `IMAGERY_GENERATE` defaults to `true` now (Gemini credits make it safe), but generation only fires as the **last** layer — explicit media, KG screenshot, external URL skip, and branded PIL card all run first. Most posts (digests, sector-tagged singles, events with links) hit a layer above and never trigger generation. Set `IMAGERY_GENERATE=false` to disable.

### Entity-impact graph imagery (built 2026-05-01)

The default imagery for catalyst-anchored posts (`single` recipe; `event` when extraction yields enough structure) is now an **entity-impact graph** — not a stock photo, not a styled text card. The reader sees the ecosystem move around the catalyst at a glance.

**`src/lib/imagery_graph.py`** has two entry points:
- `extract_graph(text, ctx) → GraphSpec | None` — calls the unified LLM (OpenAI primary, Gemini fallback) with a strict-JSON system prompt. Pulls:
  - `primary` — central entity (Figure AI, the regulator, the protocol, etc.)
  - `event` — short event tag ("Series C $1.5B", "FAA Part-450")
  - `direct` — 2-4 entities the catalyst directly affects, each with sentiment (`+|-|?`) and an optional quantitative tag (`$400M`, `+2%`, `anchor customer`)
  - `indirect` — 1-3 second-order effects with the same shape
  - Returns `None` if the LLM declines or yields no entities → caller falls through.
- `render_graph(spec, palette, out_path) → bool` — pure-PIL renderer, 1200×675 JPEG. Sector accent band, primary node centered top, two rows below for direct/indirect, sentiment-colored edges + dots, sector tag top-left, `arboryx.ai` footer. No matplotlib/networkx deps.

**Smoke result (2026-05-01):** post about *Figure AI raises $1.5B at $40B valuation, led by Microsoft + Nvidia* extracted to `primary=Figure AI`, `event=Series C $1.5B $40B`, direct=`[BMW (+/anchor customer), Harmonic Drive (+/supplier), Maxon (+/supplier), Tesla Optimus (−/rivalry)]`, indirect=`[warehouse automation (?/labor-replacement), Microsoft (+/investor), Nvidia (+/investor)]`. Rendered cleanly at 54KB.

**Where it slots in the ladder (`auto_media`):**
1. Explicit media
2. KG screenshot (configured + Playwright)
3. **Entity graph (`single`/`event` posts) ← new, preferred for content-anchored**
4. External URL skip (X auto-renders link card)
5. Branded text card (digests, events without graph-worthy structure)
6. Gemini DALL-E-style generation
7. None

The graph is preferred over both the X auto-link-card and the branded text card for `single` posts because it shows what the post actually means — *who's affected and which way* — instead of generic source imagery or just restated headline text. Cost is ~$0.0002 per extraction (gpt-4o-mini or Gemini Flash Lite); cached by `(source_id, text-prefix)` hash so repeated calls are free.

**Graceful degradation:** if `extract_graph` returns None (LLM unavailable, or post too vague to map), we fall through to the next strategy. No half-rendered graph ever lands on disk — `render_graph` validates the file exists at >5KB before returning success.

### Watermark on owned imagery (built 2026-05-02)

`src/lib/watermark.py:apply_watermark(img)` stamps a diagonal repeating "arboryx.ai" pattern over the content as the final step before save. Applied to:
- entity graphs (`imagery_graph.render_graph`)
- branded text cards (`imagery._branded_card`)
- Gemini-generated photos (`imagery._generate_image`, after the PNG → JPEG re-encode)

NOT applied to user-supplied media (frontmatter `media:`, banner.png) or KG screenshots — those originate elsewhere and shouldn't be re-stamped.

**Defaults:** white text at 5% opacity, 72px DejaVuSans-Bold, rotated -30°, repeating every 240px horizontal × 170px vertical (alternating rows offset by half a step). Visible enough that a naive crop leaves obvious damage, light enough not to fight the data.

**Env knobs (no code change to tune):**
- `WATERMARK_DISABLE=true` — skip entirely (e.g., for media that's been sold or licensed out)
- `WATERMARK_TEXT="arboryx.ai"`
- `WATERMARK_ALPHA=0.05` — 0–1 white opacity
- `WATERMARK_FONT_SIZE=72`
- `WATERMARK_ANGLE=-30`
- `WATERMARK_GAP_X=240` / `WATERMARK_GAP_Y=170`

### Manual-post helpers (`--copy`, `--show`) — built 2026-05-02

For the manual posting workflow (X API is in CreditsDepleted, paste-into-x.com is the path), `bin/post.py` adds two flags to remove friction:

- **`--copy`** — pipes the rendered text to the system clipboard. Tries `clip.exe` (WSL) → `pbcopy` (macOS) → `wl-copy` (Wayland) → `xclip` → `xsel`. For multi-part threads, parts are joined with blank-line separators so a single paste into x.com reads naturally.
- **`--show`** — opens the generated image in the system viewer. WSL: `cmd.exe /c start "" <wslpath -w>` so it picks up the default Windows photo app. macOS: `open`. Linux: `xdg-open`.

Both flags are observers — they run in addition to (not instead of) the dry-run print. `--push` still controls whether anything goes to Postiz.

Example manual workflow:
```bash
python3 bin/post.py --recipe single --tier arboryx --source-id <doc-id> --copy --show
# → text in clipboard, image opens in Photos, paste-and-attach on x.com
```

### Channel cleanup (2026-05-02)

`CHANNEL_X_BUSINESS` removed from `products/arboryx.ai/tier.config`. The underlying SolutionJet X integration (`<POSTIZ_INTEGRATION_ID_X_SOLUTIONJET>`) was soft-deleted in Postiz on 2026-04-24, so dispatching to it was always going to fail. Channel set is now `[@arboryx_ai]` only. Re-add the line with the new integration ID after reconnecting in Postiz UI when the multi-product channel strategy is built out.

### Deep-link funnel + cards.json source (built 2026-05-09)

Reframed the imagery pipeline around "X auto-renders the destination's link card from a URL in the post" rather than "we attach our own image." Drives traffic by design — every catalyst-anchored post becomes a clickable card that lands on arboryx.ai (parent) or robotics.arboryx.ai (branch).

**New modules:**
- `src/lib/sources/cards_source.py` — reads `catalyst-knowledge-graph/frontend/cards.json` directly. Each card carries `headline`, `subtitle`, `entities`, and `relationships` (with `from`/`to`/`rel`/`mechanism`/`confidence`/`impact_magnitude`) — already curated upstream.
- `src/lib/card_to_graph.py` — pure mapping from a card dict → `GraphSpec`. **Skips the LLM extraction call** the previous entity-graph path made. Per-relationship sentiment routes through `_POS_RELS`/`_NEG_RELS` whitelists; ambiguous defaults to `?`.
- `src/lib/funnel.py` — central deep-link logic:
  - `deep_link_for(tier, source_type, source_data)` formats `KG_CARD_URL_TEMPLATE` (branch) or `PARENT_URL_TEMPLATE` (parent) with the source's IDs. URL-encodes substitutions.
  - `let_x_render_link_card(tier)` reads `LET_X_RENDER_LINK_CARD` (default true; branch inherits parent).
  - `branch_enabled(parent_tier, branch_short_name)` and `branch_short_name(tier)` for the parent's `BRANCH_<NAME>_ENABLED` gate.
  - `append_link_to_text(text, link)` is idempotent (won't double-append if the link is already present).

**Recipe + dispatcher updates:**
- `recipes.recipe_single` now branches on source type (firestore | cards_json | duckdb), populates the deep link, sets `bundle.context["deep_link"]` and `bundle.context["card"]` (cards.json only — full card dict for the imagery layer to map without an LLM call), then appends the link to the post text.
- `composer.compose_finding` and `compose_catalyst` accept `max_chars` so the LLM rewrite leaves room for the appended link. Recipes call `_budget_for_link(link, total=280, separator_chars=2)` to compute the right budget — keeps the URL on the same tweet as the body so X renders the link card on the visible tweet.
- `bin/_common.integration_ids_for` enforces the branch enable gate: branch tiers consult their parent's `BRANCH_<NAME>_ENABLED` flag and return `[]` if disabled. Disabled branches can still be dry-run / drafted; only push is gated.

**Imagery ladder, updated:**
1. Explicit `bundle.media_paths`
2. **Deep-link skip** — when `ctx["deep_link"]` is set and `LET_X_RENDER_LINK_CARD=true`, return `[]` and let X render the link card.
3. KG card screenshot (Playwright; dormant)
4. **Structured card → entity graph (NO LLM)** — uses `ctx["card"]` from cards.json
5. LLM-extracted entity graph (fallback when no structured card)
6. External-URL skip
7. Branded text card (digests, sector-tagged singles)
8. Gemini-generated photo (orphan posts only)
9. None

**Tier configs:**
- `products/arboryx.ai/tier.config`:
  - `PARENT_URL_TEMPLATE="https://arboryx.ai/new_growth.html?sector={sector}&entry={entry_id}&date={date}"`
  - `LET_X_RENDER_LINK_CARD="true"`
  - `BRANCH_ROBOTICS_ENABLED="true"`, all other branches `"false"` (mirrors `arboryx-admin/frontend/arboryx_frontend.config`'s `SECTOR_*_ENABLED` pattern).
- `products/arboryx.ai/branches/robotics/tier.config`:
  - `DATA_SOURCE_1_TYPE="cards_json"` pointing at `../../../../../catalyst-knowledge-graph/frontend/cards.json` (DuckDB and `firestore_inherited` shifted to slots 2 and 3 — kept for richer queries).
  - `KG_CARD_URL_TEMPLATE="https://robotics.arboryx.ai/?card={card_id}"`.

**Smoke results (2026-05-09):**
- `bin/post.py --recipe single --tier arboryx.robotics --source-id ROB-041726-001` → 240-char Arboryx-voiced post + `https://robotics.arboryx.ai/?card=ROB-041726-001` on the same tweet, NO image attached. URL char-budget kept the post in a single tweet so X renders the destination's link card on the visible tweet (not relegated to part 2/2 of a thread, as happened in the first iteration).
- Branch-disabled gate verified: with `BRANCH_ROBOTICS_ENABLED="false"`, `integration_ids_for(robotics)` returns `[]`. Dispatcher refuses to push.
- All other recipes (`narrative`, `sector-digest`, `event`) unaffected.

**Cost saved:** every Robotics-branch single post → 0 LLM calls for imagery (was 1). Parent-tier finding-anchored posts also drop the LLM imagery call when the link card is acceptable. LLM is only retained for findings without structured relationships (Firestore parent posts that fall through ladder steps 2/4) and for sector-digest summaries.

**Next steps before flipping more branches on:**
1. Wire per-card `og:image` route on `arboryx.ai` and `robotics.arboryx.ai` (next.js or static gen), so X's auto-link-card has something visual to render. Until then, posts get text-only link cards.
2. Add cards.json equivalents for other sectors as their KG modules ship. Then flip `BRANCH_<NAME>_ENABLED="true"` per sector.

**Recipes pass context for imagery:** `PostBundle.context: dict` carries `source_url`, `sector`, `title`, `subtitle`, and (for digests) `headlines`. Each recipe populates whatever it has from its source data — `recipe_single` from the Firestore finding or DuckDB row, `recipe_sector_digest` from the Firestore query, `recipe_event` from the user-supplied title/body/link. Adding a new recipe should populate the same keys to get good imagery for free.

---

## Pre-commit secret/proprietary scrub (2026-06-28)

First-ever repo checkin. Scanned the whole tree before `git add`:
- **`.env.example` was holding *real* secrets** (JWT, X API key/secret, OpenAI key, CF tunnel token, Azure sub/tenant IDs) — sanitized all to placeholders. Real values live only in `.env` (gitignored).
- **`.gitignore` hardened**: `*.log` (top-level `errors.log` was previously uncovered), `notes.txt` (scratch w/ session ids + OAuth probe URLs), `data/imagery_cache/`. Already-ignored: `.env`, `auth/` (real CF+X tokens), `.claude/` (CF tokens + LinkedIn secret in the perms allowlist), `data/posted_log.sqlite`.
- Redacted Azure subscription/tenant GUIDs out of this file (were in the "Defaults baked into `.env`" section).
- Decision: `products/` + `docs/` committed (knowledge base, no secrets). Assets (13 MB logos/banners) committed.
- No rotation needed — repo was never pushed; sanitized before first commit.
- **Live Postiz ids moved to `.env`** (round 2): the org API key + all routing ids (customer/integration/X-account/org/tunnel UUID) were redacted from this file and from `products/**/tier.config` + `handles.yaml`, then placeholdered. `tier.config` now references them as `${POSTIZ_CUSTOMER_ID_ARBORYX}` etc.; `config_loader._parse_config` expands `${VAR}` from `.env` (unknown → `''`, so a missing id drops the channel/customer rather than leaking a token). New `.env` keys: `POSTIZ_CUSTOMER_ID_ARBORYX/_ROBOTICS`, `POSTIZ_INTEGRATION_ID_X_ARBORYX/_LINKEDIN`, `X_ACCOUNT_ID_ARBORYX` (placeholders mirrored into `.env.example`).
- **`handles.yaml` is now gitignored**; committed template is `handles.example.yaml`. (No code reads `handles.yaml` — pure reference.) Real id values are also dumped in `notes.txt` (gitignored) for hand reference.

---

## LinkedIn live + daily auto-poster (2026-07-03)

- **First live LinkedIn post shipped** to the `arboryx-ai` page (the `launch`
  narrative — banner + "Introducing Arboryx.ai"). Integration validated:
  provider `linkedin-page`, org-page scope granted, token valid (expires
  2026-07-18, has refresh token), `LINKEDIN_ENABLED=true`.
- **Root cause of the historical "LinkedIn never posts"**: NOT permissions —
  the Temporal worker pollers had silently died (orchestrator "online" but
  polling no queues), so every queued post sat in `QUEUE` forever.
  `docker compose restart postiz` re-registered workers → post published in
  seconds. See `reference_postiz_api` memory for the full debug ladder.
- **New: `bin/daily.py`** — daily auto-poster. Once/day (cron / Task Scheduler),
  per enabled tier: newest unposted primary-source item → compose → publish to
  **each channel separately** → poll `Post.state` until `PUBLISHED`/`ERROR`
  (never fire-and-forget). Failed/stuck channels (e.g. X credits) are caught,
  never crash the run, and appended to `data/manual-post-queue.md` (text +
  image path) for hand-posting; also visible errored in the Postiz UI. Stuck-in-
  QUEUE items (dead workers) are left unposted for the next run; definitive
  ERRORs are marked done. `--check` verifies worker pollers + channels;
  preview by default, `--push` to publish.
- **Operator manual: `docs/daily-posting.md`** — the how-to for running this
  cold (daily routine, where to find manual-post text/image, troubleshooting,
  scheduling).

### Makefile + heal + docker scheduler (2026-07-03, same day)

- **`Makefile`** — one entrypoint for all ops. Wraps the existing lifecycle
  scripts (`deploy`/`update`/`status`/`down`/`clean*`), compose helpers
  (`ps`/`logs`/`restart`), the daily poster (`check`/`post`/`post-preview`/
  `manual-queue`), healing, and the scheduler. `make` alone prints the list.
- **`bin/heal.sh`** (`make heal` / `heal-check`) — checks Temporal cluster
  health + whether per-provider workers are polling their task queues; restarts
  temporal and/or postiz to re-register only what's broken, then waits until
  pollers return. Automates the dead-worker fix. `--check` reports without
  restarting (exit 1 if unhealthy).
- **Docker scheduler** (`ops/scheduler/`, compose `scheduler` profile,
  `make scheduler-up`) — chosen over host cron/Task Scheduler so it piggybacks
  on the always-on docker stack. Tiny supercronic container: mounts the repo
  (`.:/app`), the docker socket (so `daily.py`/`heal.sh` exec siblings
  unchanged), and gcloud ADC read-only (Firestore). Cron fires
  `ops/scheduler/run-daily.sh` = `make heal && make post`, logged to
  `data/daily.log`. Schedule in `ops/scheduler/crontab` (default 14:30,
  `SCHEDULER_TZ` in `.env`). Survives reboots iff Docker auto-starts (Docker
  Desktop "start on login"). Opt-in via the profile so the normal stack is
  unaffected.
- **`requirements.txt` gap fixed** — added `Pillow` (imagery.py imported PIL but
  it was never listed; host had it globally, hiding the gap). Surfaced by
  smoke-testing `make check` inside the scheduler image. Gemini uses raw HTTP
  (no SDK dep); `google.cloud.firestore` covers the google imports.
- **Headless Firestore — RESOLVED.** Initial in-container failure was the user
  ADC (`503 Reauthentication is needed`). The box already has a working SA at
  `$GOOGLE_APPLICATION_CREDENTIALS`
  (`/mnt/c/.../arboryx.ai/dev-utils/service_account.json`, SA
  `market-agent-sa@marketresearch-agents`, verified datastore read). The
  scheduler now reuses that env var and **identity-mounts** the key at its own
  host path inside the container:
  `${GOOGLE_APPLICATION_CREDENTIALS:-/dev/null}:${GOOGLE_APPLICATION_CREDENTIALS:-/dev/null}:ro`
  + passes the same var. Verified: `make post-preview` composes the parent tier
  from Firestore headless in-container. No `.env`/key-copy needed since the var
  is already exported. Robotics branch (`cards_json`) needs the sibling
  `catalyst-knowledge-graph` repo, mounted at `/catalyst-knowledge-graph:ro`
  (relative `../` — resolves correctly from the main checkout, not a worktree).
- **In-container validation (all passed):** `make check` (workers polling, both
  tiers → X/LinkedIn), `make heal-check` (docker-socket exec to siblings works),
  Pillow import. Only Firestore auth is the open item above.
- **Standing rule captured**: do NOT push / open PRs without being asked — the
  earlier push of the daily-poster branch was unrequested. Commit locally, stop.
