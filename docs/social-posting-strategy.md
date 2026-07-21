# Social posting strategy — evidence + required changes

**Date:** 2026-07-20. **Status:** research complete; code changes proposed, not yet made.

## Context

The publisher currently posts high volume with per-channel budgets
(LinkedIn 12×/day, X 5×/day while draining a backlog) and **auto-@mentions the
1–2 subject companies of every story** (`HANDLE_INJECTION=true`, resolved to real
org URNs on LinkedIn and `@handle`s on X). A deep-research pass (104 agents,
3-vote adversarial verification, 2026-07-20) surfaced platform-documented rules
that conflict with two of those behaviours. This doc records the findings and the
changes they imply, separating **platform-documented** facts from
**practitioner folklore**.

## Findings

Every finding below passed 3-of-3 adversarial verification. Confidence is the
verifiers'; the doc-author's read on *what to do* is in "Required changes".

### 1. X: batching cannibalises your own posts — DOCUMENTED
X open-sourced its For You algorithm; it contains an **Author Diversity Scorer**
that exponentially decays each successive post from the same author in the
candidate set (first post full score, each next one attenuated). So firing 5 X
posts back-to-back makes them compete with each other. There is also a **~50
original-posts/day account cap** for unverified accounts (May 2026) — which is
exactly the wall we hit at 47.
Source: github.com/xai-org/x-algorithm ; docs.x.com/developer-terms/policy

### 2. X: auto-mentioning subject companies violates the Automation Rules — DOCUMENTED
X's Automation Rules permit automated @mentions **only** when the mentioned
account "requested or clearly indicated an intent to be contacted." Being the
*subject* of a post does not qualify; a follower doesn't either. "Spammy or
duplicative" automated mentions "may result in… removal of your posts from Search
or the suspension of your app or account." **This is shipped behaviour of ours on
X and is out of policy.**
Source: help.x.com/en/rules-and-policies/x-automation

### 3. Individuals: never auto-tag — DOCUMENTED (both platforms)
Auto-tagging CEOs/founders/researchers with no relationship is the
highest-report-risk behaviour. On X it directly breaks the unsolicited-mention
rule; on LinkedIn it's reportable reach-seeking spam. We don't do this today —
the rule is: never start.
Source: help.x.com/en/rules-and-policies/x-automation ; linkedin.com/help spam policy

### 4. LinkedIn: tagging isn't policy-governed, but spam is — DOCUMENTED
LinkedIn's Professional Community Policies say nothing about tag etiquette (so
"tag the company for reach" is **folklore**), but "reach-seeking, irrelevant, or
gratuitously repetitive" promotional content **is** documented, reportable spam.
High-frequency automated link posting can be classified this way.
Source: linkedin.com/legal/professional-community-policies ; linkedin.com/help spam policy

### 5. LinkedIn: link posts are the weakest format — DIRECTIONAL (vendor benchmark)
Link posts show materially lower engagement than native formats (one vendor
benchmark: 3.25% vs 5.20% average). LinkedIn says there's no *intentional* blanket
link penalty; the effect is model-learned, not a documented rule. Our
headline+deep-link cards are structurally on the weak end. Directional, not proof.
Source: socialinsider.io/social-media-benchmarks/linkedin

### 6. Communities/Groups are dead ends — DOCUMENTED
X Communities shut down **May 30, 2026** (low adoption, 80% of X's spam reports).
LinkedIn Groups route external-link posts into a **14-day admin-moderation queue**
— invisible until approved, auto-deleted if not. Neither scales for an automated
feed.
Source: techcrunch/engadget (X Communities shutdown) ; linkedin.com/help groups

### What was REFUTED (don't act on these)
The marketer staples failed verification: "post 2–5×/week for the sweet spot,"
"11+ posts/week = +17k impressions," "high frequency has no penalty." No primary
support. Also unverifiable: whether LinkedIn cannibalises a page's own rapid-fire
posts — LinkedIn doesn't open-source its algorithm, so it's absence-of-evidence,
not a green light.

## Required changes (proposed)

Ranked by how strongly the evidence backs them.

1. **Stop auto-@mentioning on X.** This is the one clear policy violation. Options:
   (a) drop entity tags on the X variant entirely, or (b) keep them only for
   companies that follow @arboryx_ai (the one case the rule allows). Simplest is
   (a). LinkedIn URN mentions can stay — not policy-governed there, though see #4.
   *Code:* gate `entity_tags`/`HANDLE_INJECTION` per channel — X already composes
   a separate variant in `channel_dispatch.channel_parts`, so this is a per-label
   suppression, not a rewrite.

2. **Cut and spread X volume.** The Author Diversity Scorer + ~50/day cap both say
   low-and-spaced. Recommend **≤5 X posts/day, ≥1–2h apart** — never batch.
   *Code:* the scheduler already supports this (`x 5` split across 08:30/20:30);
   widen to 2–3 fires of 2–3 posts and keep `DELAY` high. Consider randomised
   spacing (jitter) so it's not a robotic 90s heartbeat.

3. **Don't chase LinkedIn volume; drop steady-state cadence.** No documented
   author-diversity throttle, but link posts are weak and mass automated link
   posting is reportable. Backlog drain at 12–48/day is fine as a one-off; **steady
   state should be a few high-signal posts/day**, not dozens.

4. **(Optional) LinkedIn link-in-first-comment.** The common mitigation for the
   link-format penalty is putting the URL in the first comment, not the body.
   Directional only — worth an A/B, not a certainty. Non-trivial: needs a
   follow-up comment API call after the post publishes.

5. **Never add individual tagging.** Keep entity resolution scoped to
   organisations only. No CEO/founder handles, ever.

6. **Skip communities/groups.** Not worth building — X Communities is gone,
   LinkedIn Groups won't pass an automated feed through moderation.

## What NOT to change
- Deep links / card images stay — the link-format effect is directional and the
  card image is our main visual hook. Don't strip the thing that makes the post
  legible to chase an unproven penalty.
- LinkedIn org URN mentions can stay for now (policy-silent), but revisit if
  volume ever draws spam reports.

## Verification when implemented
- Preview an X card and confirm no `@` tags in the X variant; confirm LinkedIn
  variant unchanged: `make post-preview CHANNEL=x TIER=arboryx.robotics` vs
  `CHANNEL=linkedin`.
- Confirm scheduler X cadence is ≤5/day, spaced ≥1h, in `ops/scheduler/crontab`.
- Watch the account for the ~50/day cap; the rolling-24h X counter in the DB is
  the local guard.
