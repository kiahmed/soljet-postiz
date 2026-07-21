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

### 2. X: the prohibited pattern is UNSOLICITED BULK mentions, not factual crediting — DOCUMENTED (nuanced)
The rule's own words: "Automating mentions and replies to reach many users on an
**unsolicited basis** is an abuse… **Spammy or duplicative** use of mentions… may
result in enforcement." Read in context, this targets reach-farming (tagging
accounts that aren't in your story to fish for attention) and harassment
(continuing to tag someone who asked you to stop) — **not** factually crediting
the actual participants of a real event. A single relevant @mention of the
company a news post is genuinely about is a gray area, not the spam pattern. The
enforcement risk scales with **volume and irrelevance**, not with a tag count.
So on an "Oracle partnered with Nebius" post, tagging **both** is legitimate —
they're both subjects, both benefit, it's the citation fabric these platforms run
on. The guardrail is: tag only the story's genuine subjects, keep volume low and
spaced, never tag non-participants or individuals.
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

## Required changes

Most of this is **config or standing policy, not code** — the composer already
does the right thing (`primary_entities()` tags the story's relationship-weighted
*subjects*, capped at `MAX_ENTITY_TAGS=2`, orgs only).

### Config only (scheduler / crontab)
1. **Cut and space X cadence.** Author Diversity Scorer + the ~50/day account cap
   both say low-and-spaced. Target **≤5 X posts/day, ≥1h apart** — never batch.
   Pure `ops/scheduler/crontab` timing: e.g. 2–3 fires of 2 posts, `DELAY` high.
   Optional nicety: jitter the spacing so it's not a robotic fixed heartbeat.
2. **Drop steady-state LinkedIn cadence.** Backlog drain at 12–48/day is a fine
   one-off; steady state should be a few high-signal posts/day, not dozens (link
   posts are weak, and mass automated link posting is reportable spam). Also
   crontab, not code.

### Standing policy (no change needed — keep as is)
3. **Keep subject-company @mentions on X and LinkedIn.** Tag the story's genuine
   subjects (1–2, both parties of a partnership/deal) — this is legitimate
   crediting, drives discovery + reposts, and is already what the code does. Do
   NOT drop it or cap to one.
4. **Never tag individuals.** Entity resolution stays scoped to organisations —
   no CEO/founder/researcher handles, ever. Already true; keep it true.
5. **Skip communities/groups.** X Communities is gone; LinkedIn Groups won't pass
   an automated feed through moderation. Don't build it.

### Optional code experiment (only real code item)
6. **LinkedIn link-in-first-comment.** The usual mitigation for the weak link
   format is putting the URL in the first comment, not the body. Directional, not
   proven — worth an A/B if engagement lags. Needs a follow-up comment API call
   after the post publishes; non-trivial, defer until there's a reason.

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
