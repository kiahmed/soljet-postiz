# Engagement watcher — spec (not built yet)

**Status:** DESIGN ONLY, 2026-09-24. Nothing in this doc is implemented.
**Owner:** soljet-postiz (this repo) — the watcher itself.
**Consumer:** `arboryx-admin` (sibling repo) — the queue UI + notifications.
See `../arboryx-admin/docs/reply-queue-spec.md` for that side.

## Why this exists, and why it's a queue, not a bot

Phase 3 of `docs/social-posting-strategy.md` asked "should we build reply/
engagement automation." Checked against both platforms' current policy
(2026-09-24):

- **X**: since 2026-02-23, the API only permits a reply when the original
  post's author has explicitly **summoned** the replying account. Posting a
  cold reply to someone else's post via API is against X's own stated
  policy, not just costly.
- **LinkedIn**: 2026 API restrictions broadly push third-party automation
  of comments toward manual-only engagement.

So: **no auto-posting, on either channel, ever, for this feature.** The
watcher's only job is to *find* things worth a human's attention and put
them in a queue. A person reads the queue and comments themselves, by hand,
logged into the real account. This sidesteps both platforms' restrictions
entirely — they're restricting *automated writes*, not a human being told
where to look.

## Scope, v1: comments on our own posts only

Three watcher scopes were discussed; only the first is in v1:

| Scope | v1? | Why |
|---|---|---|
| **Comments on posts we published** | **Yes** | Already-earned engagement, not cold outreach. Lowest effort (we already have the post IDs), lowest risk (replying to someone who replied to us first reads as normal community management, not reach-farming). |
| Posts from accounts our entity-handles follow/mention | No | Needs a relevance/scoring model to avoid flooding the queue with noise — its own project. |
| Sector-wide scanning for anything relevant | No | Same scoring problem, bigger; also the use case off-the-shelf social-listening SaaS (Brand24, Mention, Sprout Social) already covers reasonably well — evaluate buying before building this scope. |

v2/v3 (the other two rows) are out of scope for this doc. If pursued later,
they're additive: same queue, same UI, a different watcher module feeding it.

## Architecture: ONE shared watcher, not one per product

Every product (arboryx.robotics today; Simmer, Matrix, Facades, and future
branches like power-and-energy or ai-chips) already posts through the
**same Postiz instance** — confirmed live:

```sql
-- real query against postiz-postgres, 2026-09-24
SELECT name, "providerIdentifier", "customerId" FROM "Integration";
--  Arboryx.ai | linkedin-page | ab2d1389-...
--  Arboryx.ai | x             | ab2d1389-...
--  Simmer     | linkedin-page | be8049c0-...
--  Matrix     | linkedin-page | be8049c0-...
--  Torque     | linkedin-page | be8049c0-...
--  Facades.trade | linkedin-page | be8049c0-...
```

Products are already segmented by `customerId` in Postiz's own DB. The
watcher's core loop — "list our published posts, check each for new
comments, enqueue the new ones" — needs **zero product-specific knowledge**
to do that part: it's the same two Postiz tables (`Post`, `Integration`)
for every product.

**What DOES need to be per-product** is *context enrichment*, so a human
reading the queue sees "this is about the Agtonomy/Kubota card" rather than
a bare comment string. That's a thin, declarative config, not a code fork —
see "Per-product config" below.

**Recommendation: one container, in this repo's docker-compose stack,
config-driven per product.** Rationale:
- Avoids N deployments of near-identical polling logic.
- The one piece of real logic (call platform's read API, diff against what
  we've already queued, write to Firestore) is 100% shared.
- New products (a `power-and-energy` branch, say) add a config entry, not a
  new service.
- Counter-argument considered and rejected: "keep it per-product so it can
  use that product's KG for scoring." Not needed for v1 — v1 doesn't score
  or filter by relevance (there's nothing to score: EVERY comment on our own
  post is worth a human glance). Scoring only becomes necessary for the v2/v3
  scopes (follows / sector-wide), which are explicitly out of scope here and
  can revisit this decision when they're built.

## Data flow

```
┌─────────────────────────────────────────────────────────────┐
│ postiz-postgres (existing, shared)                           │
│   Post (state=PUBLISHED, releaseURL, integrationId)           │
│   Integration (providerIdentifier, token, customerId)         │
└───────────────────────────┬───────────────────────────────────┘
                            │ read-only SQL, same pattern as
                            │ bin/daily.py's _psql() helper
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ engagement-watcher (NEW container, this repo)                 │
│  1. list PUBLISHED posts per product (by customerId), last N  │
│     days (configurable window — comments trickle in for       │
│     days after posting, don't stop watching at 24h)           │
│  2. per post: call the platform's OWN read API using the      │
│     Integration's stored token —                              │
│       LinkedIn: Social Actions API, GET comments on the share  │
│       X: GET /2/tweets/:id and expand replies (a READ, not     │
│          a reply — the summon restriction is on writes only)   │
│  3. diff against what's already in the Firestore queue         │
│     (dedupe on platform comment id)                             │
│  4. new comment → write a queue doc (schema below) + resolve   │
│     product context (headline/card_id from that product's      │
│     own posted_log.sqlite, keyed by the Post row's source)     │
└───────────────────────────┬───────────────────────────────────┘
                            │ Firestore writes
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ Firestore: engagement_queue/{doc_id}   (NEW collection)        │
│  read + managed by arboryx-admin — see its own spec doc         │
└─────────────────────────────────────────────────────────────┘
```

## Per-product config (`ops/watcher/products.yaml`, sketch)

```yaml
- product: arboryx.robotics
  postiz_customer_id: ${POSTIZ_CUSTOMER_ID_ROBOTICS}
  context_source: sqlite            # reads data/posted_log.sqlite (this repo)
  context_tier: arboryx.robotics    # for card_id -> headline/link lookup
- product: simmer
  postiz_customer_id: ${POSTIZ_CUSTOMER_ID_FACADES}
  context_source: none              # no per-item context available yet; queue
                                     # item shows the post text itself, no headline
- product: matrix
  postiz_customer_id: ${POSTIZ_CUSTOMER_ID_FACADES}
  context_source: none
```

Adding a product = one new entry. `context_source: none` degrades
gracefully (queue item just shows the comment + our post text, no extra
card metadata) — never a hard failure for a product without a KG.

## Firestore queue schema (`engagement_queue/{doc_id}`)

```jsonc
{
  "product": "arboryx.robotics",
  "channel": "linkedin",                 // linkedin | x
  "our_post_id": "cmu8le3f400djpw8ez2ej1448",   // Postiz Post.id
  "our_post_url": "https://www.linkedin.com/feed/update/urn:li:share:...",
  "platform_comment_id": "...",          // dedupe key, per channel's own id shape
  "commenter_name": "Jane Doe",
  "commenter_profile_url": "https://linkedin.com/in/...",
  "comment_text": "Great point about Kubota's role here...",
  "comment_posted_at": "2026-09-24T10:15:00Z",
  "context": {                           // optional, from context_source
    "card_id": "ROB-082826-007",
    "headline": "Agtonomy Scales Physical AI Platform...",
    "card_url": "https://robotics.arboryx.ai/card/ROB-082826-007"
  },
  "status": "open",                      // open | replied | dismissed | expired
  "queued_at": "2026-09-24T10:20:00Z",
  "expires_at": "2026-09-26T10:20:00Z"   // queued_at + TTL, see below
}
```

## TTL / expiry

**Default 48 hours** from `queued_at`. Rationale: a comment reply reads as
genuine engagement only while the conversation is warm; a reply to a
2-week-old comment reads as noise (or worse, makes it obvious a human
wasn't actually watching). A daily cron (or the watcher's own poll loop)
flips `status: open` → `status: expired` past the TTL — never deleted
outright, kept for a "what did we miss" retro view in the admin UI.

Configurable per product later if 48h proves wrong for a slower-moving
product; hardcode it for v1, don't build a config knob for a number nobody
has validated yet.

## What this doc does NOT cover

- Scoring/ranking queue items (v1 has no scoring — every comment on our own
  post qualifies).
- The v2/v3 scopes (follows, sector-wide) — separate future doc if pursued.
- Notification delivery and the queue UI — `arboryx-admin`'s spec.
- LinkedIn/X API rate limits for the comment-read calls — needs measuring
  against real volume before this is built, not guessed here.

## Open questions before implementation

1. Does the existing LinkedIn integration token (already used for
   `linkedin_urn.py`'s org lookups) have the scope to read comments on our
   own shares, or does that need a different permission grant?
2. X's read API for replies still costs money per the pay-per-use model
   (`$0.005/read` per the 2026 pricing) — needs a monthly budget estimate
   once real comment volume is known.
3. Where does `docker-compose.yml` best host this — a new profile like
   `scheduler`/`scheduler-gcp`, polling on its own cron, or a long-running
   poll loop? Lean toward a cron-fired one-shot (matches every other job in
   this stack) over a persistent daemon, but not decided.
