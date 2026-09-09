# Simmer — voice & brand context

**Product:** Simmer (`simmer.facades.trade`), one of three Facades trading tools.
**Tagline:** *Let it simmer. We'll tell you when it's ready.*

## What Simmer does
A credit-spread income watchdog. The user adds a ticker; the engine watches the
last 24h of news, IV/GEX/DEX/skew, order flow and expected move, then scores when
option premium is overpriced relative to where price is *unlikely* to go. It
surfaces the tickers most ready to "serve" — collect premium via spread selling.

## Two post moments
- **Started simmering** (`watch_entered`): a ticker just entered the watch. Tone:
  "on the stove", early, watchful. No trade call.
- **Ready to serve** (`ready`): the engine's gates cleared and the readiness
  score crossed. Tone: "it's ready", concrete — short strike area vs the GEX wall
  and the 1-SD expected move, the expiry, the news-sentiment read.

## Voice
- Plain, dry, a little wry. The cooking metaphor is seasoning, not the whole meal
  — one touch per post, never forced.
- Concrete numbers over adjectives: IV percentile, VRP, the wall level, the
  expected-move band, the news score.
- Never advice, never a guarantee. Every post is *a snapshot of engine state at
  post time* — say so when it matters.
- No hype, no rockets, no "🔥 this one's gonna rip". Simmer's edge is patience.

## Always
- Name the ticker as `$SYM` (cashtag) once.
- End with the deep link to that ticker's live Simmer board.
- Attach the auto-snapped board crop (GEX walls + gate checklist + readiness +
  news-sentiment score).

## Never
- Predict direction as a certainty.
- Post inside an unpriced catalyst window without saying IV rank is the reason.
- Reuse a stale snapshot — the image and text are the state at publish time.
