"""US options market-hours gate — shared by simmer-poster and matrix-poster.

Both products deal in live options data (strikes, IV, GEX walls, the strategy
grid) that's only meaningful while the market is actually trading. A post
composed from a Saturday-morning snapshot, or a Sunday-night re-read of
Friday's closing chain, reads as a live signal when it isn't — the whole
point of "a snapshot of engine state, not advice" breaks down if the state
being snapshotted is stale by a day and a half.

Regular US equity/options session: 9:30-16:00 America/New_York, Monday-Friday.

v1 has no market-holiday calendar — a holiday reads as a normal weekday, so a
handful of extra posts a year could slip through on, say, Thanksgiving. That's
a narrow, known gap (not a data-correctness bug — whatever chain the engine
read really was the last live one), left for a follow-up rather than blocking
this on a hardcoded holiday table now.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_TZ = ZoneInfo("America/New_York")
_OPEN = time(9, 30)
_CLOSE = time(16, 0)


def is_market_open(now: datetime | None = None) -> bool:
    """True during the regular US equity/options session (9:30-16:00 ET,
    Mon-Fri). `now` defaults to the real current time; pass a datetime
    (tz-aware or naive-UTC) to test a specific moment."""
    now = now or datetime.now(_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(_TZ)
    if local.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    return _OPEN <= local.time() < _CLOSE


def market_hours_enforced(tier) -> bool:
    """Tier-config escape hatch (`MARKET_HOURS_ENFORCED`, default true) so the
    gate can be turned off — local testing outside market hours, or a product
    that decides it doesn't want this gate at all."""
    # An env var wins over the tier file, so a no-traffic TEST revision on Cloud
    # Run can switch the gate off without shipping a different tier config (the
    # config is baked into the image).
    env_raw = os.environ.get("MARKET_HOURS_ENFORCED")
    raw = env_raw or tier.raw.get("MARKET_HOURS_ENFORCED", "true")
    enforced = str(raw).strip().lower() != "false"
    if env_raw is not None and not enforced:
        # Loud on purpose: this must never be true on a live revision (that
        # no-traffic TEST-revision use case above is the only reason it
        # exists), and an env var silently overriding the tier config is
        # exactly the kind of thing that should not fail quietly.
        log.warning("market-hours gate DISABLED by env (MARKET_HOURS_ENFORCED=%r) — "
                    "this must never be set on a live revision", env_raw)
    return enforced
