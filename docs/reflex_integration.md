# Facades · Reflex — posting pipeline

Reflex (facades-news-reactor) makes the finished post — image, caption, hashtags —
and stores it in **its own** Supabase (`public.best_signal_posts`). This side only
publishes it: no composer, no snap, no rewording. Upstream contract:
news-reactor `docs/reflex_events_updates.md` (§0 quick start, §4 Pub/Sub).

```
Reflex poster ──publish──▶ topic facades.reflex-signal-posts   (Reflex-owned)
                               │ attrs: product=reflex, event=post_created,
                               │        event_id=RFX-POST-<id>, post_id, …  (empty body)
                               ▼
              sub facades.reflex-signal-posts.postiz   filter product="reflex", push (OIDC)
                               ▼
                   Cloud Run reflex-poster  (bin/reflex_poster.py, runs as facades-poster-sa)
                     1. skip if event_id seen (Firestore reflex_poster_dedupe)
                     2. select … where id=post_id and not posted  FOR UPDATE SKIP LOCKED
                     3. upload image_png (+ alt text) → post per channel via Postiz
                     4. update best_signal_posts set posted=true   (the only column we write)
```

| Channel | Text |
|---|---|
| X | `caption` as-is (Reflex fits it to 280 by X's counting) |
| LinkedIn / others | `caption_text` + a hashtag paragraph |

Tags are only trimmed from the end; `#Reflex #FacadesReflex` are always kept.
Knobs live in `products/facades/reflex_tier.config`: `REFLEX_MAX_HASHTAGS_<CHANNEL>`,
`REFLEX_MAX_AGE_MINUTES` (stale-message guard, 180), `REFLEX_MAX_POSTS_PER_DAY`
(off). No scheduler — Reflex's own cadence (04:00–16:00 ET, a few a week) is the schedule.

## Pieces

| Path | What |
|---|---|
| `products/facades/reflex_tier.config` | tier (registered in `config_loader._TIER_FILE_BY_ID`) |
| `src/lib/sources/reflex_posts.py` | read / lock / mark-posted against news-reactor's DB |
| `bin/reflex_poster.py` | `--serve` (Cloud Run), `--pull` (local), `--event` (one post by id) |
| `ops/reflex/deploy.sh` | `--sa-only` (secret `reflex-db-url` from .env + IAM), `--poster-only`, `--pubsub-only`, `--sub-local` |
| `ops/reflex/preflight.sh` | every GCP + .env + DB dependency, green/red |

## .env

```
REFLEX_SUPABASE_DB_URL=      # = facades-news-reactor/.env SUPABASE_DB_URL (pooler, owner)
REFLEX_POSTS_TOPIC=facades.reflex-signal-posts
REFLEX_PUBSUB_SUBSCRIPTION=facades.reflex-signal-posts.postiz
POSTIZ_INTEGRATION_ID_X_REFLEX=
POSTIZ_INTEGRATION_ID_LINKEDIN_REFLEX=
POSTIZ_CUSTOMER_ID_REFLEX=   # the "Facades" customer
```

## Local

```bash
make reflex-event EVENT='{"post_id":5}' DRY=1     # read row, write PNG + per-channel text, no post
make reflex-event EVENT='{"post_id":5}'           # Postiz DRAFT; row stays posted=false
make reflex-event EVENT='{"post_id":5}' MODE=now  # publish + posted=true
```

## Go live

1. Connect the Reflex X / LinkedIn accounts in Postiz (customer "Facades"); put
   their ids in `.env` (`POSTIZ_INTEGRATION_ID_{X,LINKEDIN}_REFLEX`).
2. `make reflex-deploy` — redeploys the poster with the channel ids and creates the
   push subscription + dead-letter.
3. `make reflex-preflight` — all green.
