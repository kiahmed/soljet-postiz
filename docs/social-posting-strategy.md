# Social posting strategy — evidence + required changes

**Date:** 2026-07-20 (Part 1, below); **2026-09-19 (Part 2, content quality —
jump there)**. Part 1 status: mostly implemented (see Part 2's recap).

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

---

# Part 2 — Content quality & audience engagement (2026-09-19)

**Scope of this pass:** robotics (`arboryx.robotics`) only, per direction —
copy the pattern to other products once this one is proven. Method: read
this repo's actual composer code, pulled real published posts from the
Postiz DB, and read catalyst-knowledge-graph's schema/export code
**read-only** — no changes made there, as instructed. This is a single-pass
engineering read, not the 104-agent adversarial verification Part 1 had;
confidence levels are marked per finding.

Part 1 solved **"will the platform punish us."** Nothing here contradicts
it — the recap below confirms Part 1 shipped and is holding. This part is
about a different question Part 1 didn't ask: **is the post itself worth
someone's attention once it's not being punished for existing.**

## Recap — what's already there (so nothing below repeats it)

**Part 1 (platform policy/cadence), verified against the live config:**
- X cadence: `ops/scheduler/channels.conf` fires cards at 5×/day (`30
  8,11,14,17,20 * * *`, ~3h apart) — matches "≤5/day, ≥1h apart" exactly.
  One drift worth a note: the new graph poster (below) adds a 6th X post/day
  (`30 13 * * *`), so X is at 6/day counting it, not 5. Likely fine — it's a
  different post type, not backlog-card volume — but worth knowing it's no
  longer strictly ≤5.
- LinkedIn cadence: 5×/day card + 1×/day graph = 6/day, down from the
  12–48/day backlog-drain figure Part 1 flagged. Matches the spirit ("a few
  high-signal posts/day, not dozens").
- Subject-company @mentions, no individual tagging, no communities/groups:
  all still true in the code (`primary_entities()`, `_COMPANY_TYPES` scoping
  in composer.py) — nothing to redo.
- LinkedIn link-in-first-comment (Part 1's one deferred "optional
  experiment"): still not built. Still optional, still not urgent — noted so
  it isn't rediscovered as if new.

**Already shipped, not proposals anymore (docs elsewhere say otherwise — trust the code, not the doc header):**
- `docs/design-per-channel-imagery.md` is marked **"PROPOSED — not
  implemented"** but is stale — per-channel imagery (`IMAGERY_POLICY_X`/
  `IMAGERY_POLICY_LINKEDIN`, `tier.imagery_policy`) is fully live
  (`config_loader.py`, `channel_dispatch.py`, `daily.py`, `post.py`, all
  reference it). The one piece of that doc genuinely **not** built: the
  **confidence gate** (`CONFIDENCE_GATE_ENABLED`/`CONFIDENCE_GATE_MIN`) —
  zero references anywhere in this repo. That's a real, still-open item —
  picked up below (§Phase 1.2) because it's a direct quality lever ("don't
  post shaky catalysts") that was already designed and never finished.
- `docs/graph-posters.md`'s standalone catalyst-graph post
  (`compose_graph()`, the entity dependency map as its own LinkedIn+X post,
  `--kind graph`) shipped this week (`bec9e00`, plus two follow-up fixes:
  `fafbff1` per-channel thread splitting, `0f9ad27` never-retry-a-failed-
  channel). This is good, recent, real quality work — recapped, not
  reproposed. One thing worth watching, not fixing yet: a `compose_graph()`
  post over 280 chars splits into 2 X tweets; part 2/2 alone (dependency
  list + hashtags + link, no headline) reads as an orphaned diagram if seen
  out of thread context (quote-tweet, direct permalink) — exactly the
  failure mode `docs/graph-posters.md` itself warns against ("never post
  the graph alone... it needs the card's headline next to it"), just via a
  path (thread splitting) that doc didn't anticipate. Flagged, not fixed —
  low frequency, needs a real decision on whether to repeat the headline in
  every thread part or just accept it.

## The actual finding: the post text is a headline, nothing more

This is the load-bearing discovery of this pass, and it's code-verified, not
inferred. Traced the text from source to post:

`compose_catalyst()` (`src/lib/composer.py:376`) uses the KG card's own
`share.linkedin_text` / `share.twitter_text` **verbatim** as the post body —
deliberately, to avoid the LLM-fabrication risk a rewrite would carry (a
documented, correct design decision, see the comment at composer.py:46-48).
But those fields, read straight from catalyst-knowledge-graph's own
`src/export.py`:

```python
def _twitter_text(headline, entities):
    ...  # headline + up to 3 $TICKERs
def _linkedin_text(headline, subtitle, source_url):
    return "\n".join([headline, "", subtitle, "", source_url])
```

**are the headline restated, plus tickers or a source link.** No synthesis,
no reasoning, no "why," no comparison. Whatever "insight" a post carries
today comes entirely from what Postiz wraps around that headline —
hashtags, the deterministic forward-hook (`_relationship_hook`), and the
temporal lead-in. Confirmed against 6 real published posts (query below):
every one is `<headline> #tags @mentions` + a hook line + the link. The hook
varies well ("Who's supplying whom", "How we read the rivalry", "See who's
consolidating" — this part is genuinely good, don't touch it) but the body
itself never explains anything.

```sql
select left(content,400) from "Post"
where "integrationId" in ('cmpd49hxe0001n076cwi113lo','cmocawx900003pv7rml68uos4')
and state='PUBLISHED' order by "createdAt" desc limit 6;
```

Two consequences worth naming plainly:

1. **A reader gets zero reasoning without clicking through.** The one thing
   that would make a post itself worth engaging with — *why does this
   matter* — is locked behind the deep link, every time.
2. **The `mechanism` field is sitting right there, unused, already in the
   payload this repo receives.** Every relationship on every card carries a
   `mechanism` string — a real, specific, already-written sentence. Sampled
   from the live export (`data/exports/cards.json`, read-only):

   > "ABB Robotics and NVIDIA co-published a white paper defining
   > 'Autonomous Versatile Robotics' (AVR) as a new industry benchmark,
   > building on their strategic partnership."

   This repo already partially trusts this field — `card_to_graph.py`
   pulls it for the graph node's tooltip, clipped to **14 characters**
   (`_short_note`, `mechanism.split(".")[0][:14]` — "co-published a", a
   fragment, not a note). `compose_catalyst()` never reads it at all.

## Executive takeaways

1. **The composer's craft (hooks, hashtags, temporal framing, cadence) is
   already good — the raw material it's dressed around is thin.** Fixing
   that raw material is the highest-leverage, lowest-risk lever available,
   and it requires **zero changes to catalyst-knowledge-graph** — the data
   is already in the payload this repo reads.
2. **One field — `mechanism` — is the single biggest unlock.** Surfacing a
   trimmed `mechanism` sentence (not the 14-char fragment already in use
   elsewhere) turns "ABB and NVIDIA define new benchmark" into "...
   co-published a white paper defining a new autonomous-robotics standard,
   building on their partnership" — a concrete claim instead of a label.
   Deterministic, sourced, no LLM, no new risk — same design contract
   `compose_catalyst()` already holds itself to.
3. **Two sector-level stats are computed and completely unused**:
   `top_chokepoint_entity` (most-connected entity, all-time) and
   `fastest_accelerating_relationship` (most common relationship type,
   last 14 days) — real, live numbers in every export, sitting in
   `stats{}`, referenced by zero code in this repo. Honest caveat: despite
   the names, these are coarse counts (no growth-ratio math, no baseline
   comparison) — pitch them as "state of the sector," not as a dramatic
   trend claim.
4. **The genuinely dramatic version of #3 — `graph_insights[]`
   ("NVIDIA: 12 new partnerships in 30 days, 3.2× prior quarter") — is
   real in the schema but not yet built.** catalyst-knowledge-graph's own
   `src/export.py` ships it as `"graph_insights": []  # populated when
   detectors land (Phase 1 W4)`. This is the highest-ceiling engagement
   lever found in this whole pass — a comparative, superlative,
   inherently-shareable claim — but it lives entirely on the other side of
   the line we're not crossing today. Documented here so it's on record as
   the #1 ask **for** that repo, not attempted **in** it.
5. **Backlog drain has an emergent side effect on the feed's voice.**
   `_temporal_frame()`'s "Back in mid August:" lead-in was designed as an
   occasional device for a stale item. With the backlog being drained
   newest-unposted-last (oldest first, per the existing selection order),
   it's currently the opening of *most* posts — the account can read as
   "always behind" rather than live. Not a bug in the mechanic itself
   (it's honest — the item really is old); the side effect of *how much*
   of the feed it now touches is the issue.
6. **The pasted engagement note (`docs/arboryx_boosting_engmt_txt`) names a
   real, distinct axis — replying into others' threads to borrow
   audience — but its specific claims don't clear Part 1's evidence bar.**
   "Free accounts get near-zero reach on links," "6×/15× Premium
   multiplier," exact numbers — no citations, contradicts nothing Part 1
   verified but isn't verified itself either. Treat the **structural idea**
   (the pipeline is 100% broadcast, 0% reply/engagement-with-others) as
   worth a real decision; treat the specific numbers as folklore until
   someone runs Part 1's kind of check on them. This is also a different
   kind of change than 1-5: it needs an editorial human in the loop
   (replying into strangers' threads isn't something to fully automate
   without judgment), not just a composer edit — scoped separately below.

## Step-by-step implementation plan

**Phase 1 — this repo only, no catalyst-knowledge-graph changes, ships fast:**

1. **Surface `mechanism` in `compose_catalyst()`.**
   `_top_relationship(card)` already exists and already ranks by
   confidence/evidence/impact (composer.py:119) — reuse it (don't
   re-derive "the top relationship" a second way). Add a
   `_mechanism_clause(card, max_chars=140)` that takes
   `_top_relationship(card).get("mechanism")`, cuts at a sentence boundary
   under the budget, and returns `None` if there's no relationship or no
   mechanism text (fail closed, exactly like `_relationship_hook`). Insert
   it in `compose_catalyst()` between the headline and the hashtags/hook —
   one new sentence, not a rewrite of the headline. Verify with
   `make post-preview TIER=arboryx.robotics` before touching `--push`;
   spot-check 5-10 previews for sentence-boundary truncation quality.
2. **Finish the confidence gate.** It's already designed
   (`design-per-channel-imagery.md` §"Optional feature") and never built —
   add `CONFIDENCE_GATE_ENABLED`/`CONFIDENCE_GATE_MIN` to `config_loader.py`,
   gate the newest-unposted scan in `daily.py` on the card's max relationship
   confidence when the tier has the signal. Default **OFF** — this is a
   quality floor to turn on deliberately, not a silent behavior change.
3. **A recurring "state of the sector" post**, same mold as the graph
   poster (own `--kind`, own schedule row in `channels.conf`, deterministic
   template, no LLM): pull `stats.top_chokepoint_entity` +
   `fastest_accelerating_relationship` from the export, once a week. Keep
   the copy honest per takeaway #3 ("NVIDIA remains robotics' most-connected
   name" / "Partnership announcements are outpacing every other deal type
   this fortnight") — don't imply a trend calculation that isn't there.
4. **Ease off "Back in <when>:" dominance.** Two independent levers, pick
   one to start: (a) in the daily selection, interleave a recent item
   before the oldest backlog item when both are ready, so the feed isn't
   100% retrospective while backlog drains; or (b) if the backlog is close
   to drained, just let it finish — check `pending_ids_for()`'s count for
   `arboryx.robotics` first, this might resolve itself in weeks without a
   code change.
5. **Thread-part self-containment** (low priority, low effort): when
   `split_for_thread` produces >1 part for a channel, prefix parts after
   the first with the entity name so a part seen out of context still says
   what it's about. One line in `split_for_thread` or at the call site.

**Phase 2 — proposed for catalyst-knowledge-graph, not built here:**

6. Build the `graph_insights[]` detectors that repo's own
   `technical_spec.md` already specs (chokepoint growth-ratio, narrative
   velocity vs. prior period) — the highest-ceiling item in this whole
   analysis, already designed, just not implemented. Hand this doc's
   §Executive-takeaways-4 to whoever owns that repo's backlog.
7. Optional, lower priority once Phase 1.1 ships: richer `share.linkedin_text`
   at the source (a deterministic `mechanism`-based line, not an LLM
   rewrite) — mostly superseded by doing it in `compose_catalyst()` instead,
   which needed no cross-repo coordination. Only worth revisiting if other
   consumers of `share.*` (outside this repo) would also benefit.

**Phase 3 — a product decision, not a sprint item:**

8. Whether to build a reply/engagement-with-others capability at all. Real
   axis, real potential, genuinely different risk profile (this repo posts
   to its own accounts today; replying means interacting with other
   people's content, which needs an editorial policy — what counts as
   "genuinely helpful," how to avoid looking like reach-farming under the
   same X rule Part 1 already found) before any automation. If this is
   wanted, treat it as its own design doc with the same adversarial-
   verification rigor Part 1 used for the specific claims — not as an
   extension of Phase 1's checklist.

## Applying this to Simmer / Matrix later

Once Phase 1 is proven on robotics, the same pattern generalizes:
- Simmer/Matrix's `compose_simmer()`/`compose_matrix()` already synthesize
  real numbers into text (IV%, VRP, composite score, tag-derived plain-
  English clauses) — they're actually ahead of robotics' pre-Phase-1 state
  in this one respect (see `docs/simmer_integration.md` §Text/copy,
  `docs/matrix_integration.md` §Text/copy). The open question for those two
  is less "surface a hidden mechanism field" (there isn't an equivalent
  today) and more Phase 1's other levers: a confidence/significance floor
  (Matrix already has one — the min-gap safety net; Simmer doesn't), and
  whether either would benefit from an occasional "state of the book"
  digest post the way robotics' graph poster and this doc's §3 sector-stats
  idea do.
