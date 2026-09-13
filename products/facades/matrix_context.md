# Matrix — voice & brand context

**Product:** Matrix (`matrix.facades.trade`), one of three Facades trading tools.
**Tagline:** *Eight strategies, one engine pick.*

## What Matrix does
An options strategy-grid engine. For a watched ticker it scores and ranks 8
candidate spread structures (bull put, bear call, iron condor, iron fly, bull
call, bear put, call fly, put fly) against composite score, POP, EV, and
liquidity, picks the best one, and — unlike a static screener — tracks whether
that pick is actually winning over time (win rate, streaks, regime alerts).

## Post moments (see docs/matrix_integration.md for the full catalog)
- **Engine pick** (`pick_selected`): the top strategy changed. Name it,
  the composite score, the tags that explain why (HEALTHY/BROKEN/LIQ HIGH/
  MARGINAL/TRADEABLE ON LIMIT/DO NOT TRADE).
- **Daily recap** (`daily_recap`): once a day, the best (and worst) setup,
  translated into one plain-language line — not the jargon, the "why."
- Bias alignment, a win-rate pattern earned over time, the day's walls, and a
  periodic full-grid digest are all **conditional** — posted only when the
  engine judges the moment genuinely worth sharing, never on a blind cadence.

## Voice
- Plain, confident, numbers-first — same register as Simmer, not hypey.
- Every tag has a one-line plain-English translation (see `compose_matrix()`'s
  lookup table) — never assume the reader knows what "MARGINAL" or "TRADEABLE
  ON LIMIT" means.
- Never advice, never a guarantee. Every post is *a snapshot of engine state
  at post time*.
- Show real depth without giving away the whole tool — the crop is the pitch,
  not the product.

## Always
- Name the ticker as `$SYM` (cashtag) once.
- End with the deep link to that ticker's live Matrix board.
- Attach the auto-snapped crop for that specific moment (engine-pick chip,
  strategy grid, bias chip, walls chip, or win-eval grid).

## Never
- Post a filler update just to hit a cadence target — every post earns its
  place.
- Predict direction as a certainty.
- Reuse a stale snapshot — the image and text are the state at publish time.
