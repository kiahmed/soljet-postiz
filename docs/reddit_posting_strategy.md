# Reddit posting strategy

**Date:** 2026-10-07. **Status:** proposal — nothing posts to Reddit yet. The
Devvit app is live on r/arboryx_ai and r/facades_trade_tools (`make
reddit-deploy`); the `reddit-poster` below is not built.

## Why Reddit is different

X and LinkedIn reward a steady broadcast. Reddit punishes it: a feed of
announcements reads as ads, gets downvoted, and pulls no one into the comments.
What works is explanation ("how does this work?"), honest results (wins **and**
losses), and posts that end in a real question. So Reddit gets a **small,
curated subset** of what each product already emits — not every event.

Rules for every post:

- **Volume:** at most ~1 post/day per subreddit, across all products sharing it.
- **Title:** a plain statement or question that makes a point. No hashtags, no
  emoji runs, no "🚀".
- **Body:** explain the *why* and the numbers; product link once, at the end.
- **Honesty:** include misses and losses. Cherry-picked wins kill trust fast.
- **Not advice:** Facades posts carry the line "Not financial advice."
- **Ask something:** end with one specific question to start the thread.
- Posts appear as the app account (u/soljet-postiz-intg), not u/solutionjet.

## What goes to Reddit, per product

Source column = what that product already publishes today (no new publisher).

| Product → sub | Source today | Post to Reddit | Skip on Reddit |
|---|---|---|---|
| **Robotics** → r/arboryx_ai | Firestore catalyst cards + KG stats (scheduled, no Pub/Sub) | **Weekly "state of the robotics graph"** (graph_stats: top chokepoint, fastest-accelerating relationship). **1–2 deep-dive cards/week** — highest confidence (≥0.85), explaining the card's `mechanism` ("why X depends on Y"), ending with a question | Routine cards (5/day elsewhere), daily graph posts, "Back in…" backlog items |
| **Arboryx parent** → r/arboryx_ai | Firestore `findings` (manual recipes) | **Weekly sector digest** (one sector, rotating) | Single findings |
| **Simmer** → r/facades_trade_tools | Pub/Sub `facades.ticker-events` (`product=simmer`) | **`ready` only**, written as a setup breakdown: score, short strike vs GEX wall, 1-SD expected move, what would invalidate it | `watch_entered`, `score_crossed`, `dropped`, `expiry_near` |
| **Matrix** → r/facades_trade_tools | Pub/Sub `facades.matrix-events` (`product=matrix`) | **`daily_recap`** (best/worst setup in plain English) and **`grid_digest`** (8 strategies compared — teaches the "why"). **Weekly results post: wins and losses** | `pick_selected`, `bias_aligned/diverged`, `session_open`, wins-only `pick_result` |
| **Reflex** → r/facades_trade_tools | Pub/Sub `facades.reflex-signal-posts` (`product=reflex`) | **Friday weekly scorecard**, misses included | Individual signal posts (they read as ads) |
| **Every sub** | `products/*/narratives/` | **One pinned "How it works / methodology" post** per subreddit, posted first | — |

r/facades_trade_tools is shared by three products, so the ~1/day cap is
shared: Matrix recap and Simmer `ready` compete for the slot; weekly posts
(Matrix results, Reflex scorecard) get their own days.

## Action plan

1. **Per-product Reddit rules in each `tier.config`** — which kinds/states go to
   Reddit and the cap, eg `REDDIT_POST_KINDS="ready"`, `REDDIT_MAX_PER_DAY=1`.
   Empty `REDDIT_POST_KINDS` = no Reddit for that product.
2. **One Cloud Run `reddit-poster`** (Python, same shape as the
   simmer/matrix/reflex posters):
   - **Event products:** new push subscriptions on the existing topics —
     `facades.ticker-events.reddit`, `facades.matrix-events.reddit`,
     `facades.reflex-signal-posts.reddit` — so it sees the same events the X
     and LinkedIn posters do, without touching them.
   - **Robotics/Arboryx weekly posts:** a Cloud Scheduler job (they have no
     Pub/Sub topic).
   - Reddit-specific text (title + markdown body, link last), dedupe in
     Firestore `reddit_poster_dedupe`, then `/external/post` on the Devvit app.
3. **Write the two pinned "How it works" posts** and post them first.
4. **Run in draft / dry-run** first so the Reddit text can be reviewed, then
   switch to live.
5. **Later:** surface new comments to a human (via `/external/comments`) so
   replies come from a person, not a bot.

## Open questions

- Is ~1/day per sub too much at launch? Starting at 3–4/week is safer.
- Should Matrix/Reflex losses be posted individually or only in the weekly
  results? (Proposal: weekly only.)
