#!/usr/bin/env python3
"""Reflex (Facades) publisher — picks up a finished Reflex post and publishes it.

Forked from bin/matrix_poster.py's shell (dedupe, push/pull/event modes,
always-ack), minus everything that composes or renders: Reflex
(facades-news-reactor) already made the image, caption and hashtags. This
process never rewords the text or redraws the image — it reads the row, picks
the right text field per channel, attaches the PNG, posts, and flips `posted`.

Trigger: Reflex's own topic facades.reflex-signal-posts (project
marketresearch-agents). Our subscription: facades.reflex-signal-posts.postiz
(push -> Cloud Run reflex-poster), plus a -local PULL sub for local runs.

Event shape (Pub/Sub message attributes, body is empty):
    product   "reflex"
    event     "post_created"            (the only event today)
    event_id  "RFX-POST-<post_id>"      (deterministic -> our dedupe key)
    post_id   "5"                       (PK in public.best_signal_posts)
    table, symbol, direction, move_bps, minutes_to_peak, armed_at,
    created_at, news_id, episode_id, provider

The post lives in news-reactor's Supabase (NOT EdgeLane's) — see
src/lib/sources/reflex_posts.py. Per channel:
    X          `caption` as-is (Reflex fits it to 280 by X's counting)
    others     `caption_text` + a hashtag paragraph
Tags are only ever trimmed from the END, and #Reflex #FacadesReflex are kept.

Modes:
    --serve            HTTP server for Pub/Sub PUSH delivery  (Cloud Run default)
    --pull [--max N]   pull-drain the subscription once       (local / catch-up)
    --event '<json>'   process one inline event, e.g. '{"post_id":5}'

Safety: --mode defaults to `draft` (lands unpublished in Postiz, and the row is
left posted=false so the real publish still happens). --mode now publishes and
marks the row posted. --dry-run reads the row read-only, writes the PNG + the
per-channel text locally, and never calls Postiz or writes the DB.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from bin._common import integration_ids_for, load_dotenv  # noqa: E402
from src.lib.channel_dispatch import channel_label  # noqa: E402
from src.lib.config_loader import load_tier  # noqa: E402
from src.lib.postiz_client import PostizClient  # noqa: E402
from src.lib.sources.reflex_posts import ReflexPostsDB  # noqa: E402
from src.lib.thread import max_chars_for_channel  # noqa: E402
from src.lib import posted_log  # noqa: E402

TIER_ID = "reflex"
BRAND_TAGS = ("#reflex", "#facadesreflex")      # never trimmed (case-insensitive)
MEDIA_DIR = REPO_ROOT / "data" / "imagery_cache" / "reflex"
ET = ZoneInfo("America/New_York")


def _log(event: str, **kw) -> None:
    print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                      "event": event, **kw}, default=str), flush=True)


def _as_obj(resp):
    """Postiz endpoints return either a dict or a single-element list."""
    if isinstance(resp, list):
        return resp[0] if resp and isinstance(resp[0], dict) else {}
    return resp if isinstance(resp, dict) else {}


def _int_knob(tier, key: str) -> int:
    try:
        return int(float(tier.raw.get(key) or 0))
    except ValueError:
        return 0


# --------------------------------------------------------------------- dedupe
class Dedupe:
    """Idempotency guard keyed on event_id (RFX-POST-<id>). Firestore when GCP
    is reachable, else a local JSON file. The DB's own `posted` flag + row lock
    is the second guard; this one also covers a post that went out but whose
    `posted` write failed. Also keeps the optional per-ET-day post counter."""

    def __init__(self, tier):
        self.project = (tier.raw.get("REFLEX_PUBSUB_PROJECT")
                        or os.getenv("GCP_PROJECT") or "").strip()
        self.coll = os.getenv("REFLEX_DEDUPE_COLLECTION", "reflex_poster_dedupe")
        self._fs = None
        if self.project and os.getenv("REFLEX_DEDUPE_BACKEND", "auto") != "local":
            try:
                from google.cloud import firestore
                self._fs = firestore.Client(project=self.project)
            except Exception as e:  # noqa: BLE001
                _log("dedupe_firestore_unavailable", error=str(e))
        self._path = REPO_ROOT / "data" / "reflex_poster_dedupe.json"
        self._day_path = REPO_ROOT / "data" / "reflex_poster_counts.json"

    def seen(self, k: str) -> bool:
        if self._fs is not None:
            try:
                return self._fs.collection(self.coll).document(k).get().exists
            except Exception as e:  # noqa: BLE001
                _log("dedupe_read_failed", error=str(e))
                return False
        try:
            return k in set(json.loads(self._path.read_text()))
        except (OSError, json.JSONDecodeError):
            return False

    def mark(self, k: str, meta: dict) -> None:
        if self._fs is not None:
            try:
                self._fs.collection(self.coll).document(k).set(
                    {**meta, "at": datetime.now(timezone.utc).isoformat()})
                return
            except Exception as e:  # noqa: BLE001
                _log("dedupe_write_failed", error=str(e))
        try:
            cur = set(json.loads(self._path.read_text()))
        except (OSError, json.JSONDecodeError):
            cur = set()
        cur.add(k)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(sorted(cur)))

    # ---- published-post counters (REFLEX_MAX_POSTS_PER_DAY / _WEEK) -------
    # Keys are ET calendar periods: "day:2026-09-30", "week:2026-W40" (ISO
    # week, Mon-Sun). Only signal posts count — the weekly recap doesn't.
    @staticmethod
    def periods() -> dict[str, str]:
        now = datetime.now(ET)
        y, w, _ = now.isocalendar()
        return {"day": f"day:{now:%Y-%m-%d}", "week": f"week:{y}-W{w:02d}"}

    def count(self, key: str) -> int:
        if self._fs is not None:
            try:
                doc = self._fs.collection(self.coll).document(f"__count__:{key}").get()
                return int((doc.to_dict() or {}).get("count", 0)) if doc.exists else 0
            except Exception as e:  # noqa: BLE001
                _log("count_read_failed", key=key, error=str(e))
                return 0
        try:
            return int(json.loads(self._day_path.read_text()).get(key, 0))
        except (OSError, json.JSONDecodeError, ValueError):
            return 0

    def bump(self) -> None:
        keys = list(self.periods().values())
        if self._fs is not None:
            try:
                from google.cloud import firestore
                for k in keys:
                    self._fs.collection(self.coll).document(f"__count__:{k}").set(
                        {"count": firestore.Increment(1)}, merge=True)
                return
            except Exception as e:  # noqa: BLE001
                _log("count_write_failed", error=str(e))
        try:
            cur = json.loads(self._day_path.read_text())
        except (OSError, json.JSONDecodeError):
            cur = {}
        cur = {k: int(cur.get(k, 0)) + 1 for k in keys}   # only current periods matter
        self._day_path.parent.mkdir(parents=True, exist_ok=True)
        self._day_path.write_text(json.dumps(cur))


# ------------------------------------------------------------------- event I/O
def normalize_event(raw: dict) -> dict:
    """Accept either a bare event dict or a Pub/Sub PUSH envelope
    {message:{data:<b64 json>, attributes:{...}}}."""
    msg = raw.get("message") if isinstance(raw, dict) else None
    if isinstance(msg, dict):
        attrs = dict(msg.get("attributes") or {})
        body = {}
        if msg.get("data"):
            try:
                body = json.loads(base64.b64decode(msg["data"]).decode("utf-8"))
            except Exception:  # noqa: BLE001
                body = {}
        merged = {**body, **attrs}          # attributes win (they're the routing keys)
        merged.setdefault("message_id", msg.get("messageId") or msg.get("message_id"))
        return merged
    return dict(raw)


def _post_id(evt: dict) -> int | None:
    for v in (evt.get("post_id"), str(evt.get("event_id") or "").rpartition("RFX-POST-")[2]):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            continue
    return None


# --------------------------------------------------------------- composition
# X's weighted length (twitter-text v3): these code-point ranges weigh 1, all
# else (CJK, emoji, …) weighs 2. Reflex already fits `caption` this way — we
# only re-check it so a trimmed/fallback rebuild can't go over.
_X_LIGHT = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))


def x_len(s: str) -> int:
    return sum(1 if any(a <= ord(ch) <= b for a, b in _X_LIGHT) else 2 for ch in s)


def _is_brand(tag: str) -> bool:
    return tag.lower() in BRAND_TAGS


def trim_tags(tags: list[str], *, max_tags: int = 0, fits=None) -> list[str]:
    """Drop tags from the END (never a brand tag) until len <= max_tags (if set)
    and fits(tags) is true (if given)."""
    out = list(tags)

    def ok(t):
        return (not max_tags or len(t) <= max_tags) and (fits is None or fits(t))

    while not ok(out):
        idx = next((i for i in range(len(out) - 1, -1, -1) if not _is_brand(out[i])), None)
        if idx is None:
            break
        out.pop(idx)
    return out


def _base_text(row: dict) -> str:
    text = (row.get("caption_text") or "").strip()
    if text:
        return text
    # Older rows (pre caption_text): strip the trailing hashtag run off caption.
    return re.sub(r"(\s+#\w+)+\s*$", "", (row.get("caption") or "").strip())


def _truncate(text: str, budget: int, measure) -> str:
    if measure(text) <= budget:
        return text
    while text and measure(text + "…") > budget:
        text = text[:-1]
    return text.rstrip() + "…"


def compose_for_channel(tier, label: str, row: dict) -> str:
    tags = [t for t in (row.get("hashtags") or []) if t]
    caption = (row.get("caption") or "").strip()
    max_tags = _int_knob(tier, f"REFLEX_MAX_HASHTAGS_{label.upper()}")

    if label.upper() == "X":
        # Reflex's caption is the post. Rebuild only if a tag cap is set or it
        # somehow measures over 280.
        if caption and x_len(caption) <= 280 and (not max_tags or len(tags) <= max_tags):
            return caption
        text = _base_text(row)
        kept = trim_tags(tags, max_tags=max_tags,
                         fits=lambda t: x_len(f"{text} {' '.join(t)}".strip()) <= 280)
        tag_str = " ".join(kept)
        text = _truncate(text, 280 - (x_len(tag_str) + 1 if tag_str else 0), x_len)
        return f"{text} {tag_str}".strip()

    limit = max_chars_for_channel(label)
    text = _base_text(row) or caption
    kept = trim_tags(tags, max_tags=max_tags,
                     fits=lambda t: len(text) + 2 + len(" ".join(t)) <= limit)
    return f"{text}\n\n{' '.join(kept)}" if kept else text


def alt_text(row: dict) -> str:
    """Alt text from the row's facts, e.g. "Chart: SPY rose 42 bps in 10 minutes
    after the headline '…'. Left: …". X caps alt at 1000 chars."""
    bearish = str(row.get("direction") or "").lower().startswith("bear")
    verb = "fell" if bearish else "rose"
    try:
        bps = f"{float(row.get('move_bps')):.0f}"
    except (TypeError, ValueError):
        bps = "?"
    try:
        mins = max(1, round(float(row.get("minutes_to_peak"))))
    except (TypeError, ValueError):
        mins = None
    span = f" in {mins} minute{'s' if mins != 1 else ''}" if mins else ""
    usd = ""
    try:
        usd = f" (${float(row.get('move_usd')):.2f} per share)"
    except (TypeError, ValueError):
        pass
    head = " ".join(str(row.get("headline") or "").split())
    tail = (". Left: Reflex's signal card. Right: SPY 1-minute candles "
            "with entry and peak marked.")
    lead = f"Chart: SPY {verb} {bps} bps{usd}{span} after the headline '"
    room = 1000 - len(lead) - len("'") - len(tail)
    if len(head) > room:
        head = head[:max(0, room - 1)].rstrip() + "…"
    return f"{lead}{head}'{tail}"


def _write_png(event_id: str, png: bytes | None) -> Path | None:
    if not png:
        return None
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    p = MEDIA_DIR / f"{event_id}.png"
    p.write_bytes(png)
    return p


def _float_knob(tier, key: str) -> float:
    try:
        return float(tier.raw.get(key) or 0)
    except ValueError:
        return 0.0


# ------------------------------------------------------------ promo gating
# These posts are a promo teaser, not the product: only the standout moves go
# out, and only a few a week. Everything else stays posted=false in Reflex's
# table and feeds the weekly scorecard (run_recap).
def premium_gain(row: dict) -> tuple[str, float] | None:
    """(type, gain_pct) from Reflex's recorded `premium` — the 0DTE ATM
    contract's last trade at arming vs at SPY's peak (bottom, if bearish).
    None when the row carries a `note` instead (armed pre-market, contract
    expired, no trades). Never computed or estimated here — Reflex's rule."""
    p = row.get("premium")
    if not isinstance(p, dict) or p.get("note") or p.get("gain_pct") is None:
        return None
    try:
        return str(p.get("type") or "call"), float(p["gain_pct"])
    except (TypeError, ValueError):
        return None


def premium_line(tier, row: dict) -> str | None:
    """"0DTE ATM call +27.9%" — gain_pct quoted as-is. Not part of Reflex's
    caption, so it rides alongside it (see with_cta)."""
    if str(tier.raw.get("REFLEX_SHOW_PREMIUM") or "true").lower() != "true":
        return None
    pg = premium_gain(row)
    if not pg:
        return None
    kind, gain = pg
    raw = (row.get("premium") or {}).get("gain_pct")
    return f"0DTE ATM {kind} {'+' if gain >= 0 else ''}{raw}%"


def quality_gate(tier, row: dict) -> str | None:
    """None if the row is big enough to post, else the reason it isn't.
    Uses Reflex's recorded premium.gain_pct when the post has one; a post with
    only a premium `note` (pre-market arm, expired contract) falls back to the
    SPY move in bps — blank REFLEX_MIN_MOVE_BPS to require a premium."""
    min_gain = _float_knob(tier, "REFLEX_MIN_PREMIUM_GAIN_PCT")
    pg = premium_gain(row)
    if min_gain and pg:
        return None if pg[1] >= min_gain else \
            f"0DTE {pg[0]} {pg[1]:g}% < {min_gain:g}% (REFLEX_MIN_PREMIUM_GAIN_PCT)"
    min_bps = _float_knob(tier, "REFLEX_MIN_MOVE_BPS")
    if min_gain and not min_bps and not pg:
        return "no recorded premium and REFLEX_MIN_MOVE_BPS is blank"
    bps = float(row.get("move_bps") or 0)
    if min_bps and bps < min_bps:
        return f"move {bps:.1f} bps < {min_bps:g} bps (REFLEX_MIN_MOVE_BPS)"
    return None


def cap_reason(tier, dedupe: Dedupe) -> str | None:
    """None if under both caps, else which cap is full. ET calendar day and
    ISO week (Mon-Sun); only published signal posts count."""
    periods = dedupe.periods()
    for per, knob in (("day", "REFLEX_MAX_POSTS_PER_DAY"), ("week", "REFLEX_MAX_POSTS_PER_WEEK")):
        cap = _int_knob(tier, knob)
        if cap:
            n = dedupe.count(periods[per])
            if n >= cap:
                return f"{per} cap: {n}/{cap} already posted ({knob})"
    return None


def with_cta(tier, label: str, text: str, extra: str | None = None) -> list[str]:
    """Post parts: Reflex's text untouched, then `extra` (the premium line)
    and the subscribe link. X: extra joins the main tweet only if it still
    fits 280, else it leads the self-reply; the link is always in the reply
    (a link in the main tweet costs reach). Other channels: last lines.
    The link is off while REFLEX_CTA_URL is blank."""
    url = (tier.raw.get("REFLEX_CTA_URL") or "").strip()
    cta = ((tier.raw.get("REFLEX_CTA_TEXT") or "Live Reflex calls, as they fire: {url}")
           .format(url=url) if url else None)
    if label.upper() != "X":
        tail = "\n".join(x for x in (extra, cta) if x)
        return [f"{text}\n\n{tail}" if tail else text]
    main, reply = text, []
    if extra:
        if x_len(f"{text}\n{extra}") <= 280:
            main = f"{text}\n{extra}"
        else:
            reply.append(extra)
    if cta:
        reply.append(cta)
    return [main, "\n".join(reply)] if reply else [main]


# ------------------------------------------------------------ weekly recap
RECAP_TAGS = ["#Reflex", "#FacadesReflex", "#SPY", "#StockMarket"]


def compose_recap(tier, rows: list[dict]) -> str | None:
    """The scorecard text from every post Reflex made this week (posted or
    not — it's the volume that sells the subscription, not the single call).
    Reflex only stores confirmed moves, so this counts calls that paid, never
    a win rate. None when there are too few to be worth a post."""
    moves = [float(r["move_bps"]) for r in rows if r.get("move_bps") is not None]
    if len(moves) < max(1, _int_knob(tier, "REFLEX_RECAP_MIN_POSTS")) or not moves:
        return None
    best = rows[0]                                  # since() sorts by move_bps desc
    avg = sum(moves) / len(moves)
    gains = [g[1] for g in map(premium_gain, rows) if g]   # recorded only, never estimated
    extra = f" Best 0DTE ATM contract: +{max(gains):g}%." if gains else ""
    head = " ".join(str(best.get("headline") or "").split())
    tags = " ".join(RECAP_TAGS)

    def build(h: str) -> str:
        return (f"📊 Reflex this week: {len(moves)} SPY move{'s' if len(moves) != 1 else ''} "
                f"called off the headlines, avg {avg:.0f} bps in the called direction. "
                f"Biggest: {float(best['move_bps']):.0f} bps after “{h}”.{extra} "
                f"Subscribers got every call live. {tags}")

    if x_len(build(head)) > 280:
        while head and x_len(build(head.rstrip() + "…")) > 280:
            head = head[:-1]
        head = head.rstrip() + "…"
    return build(head)


# --------------------------------------------------------------------- publish
def process_event(raw_evt: dict, *, tier, dedupe: Dedupe, db: ReflexPostsDB | None = None,
                  mode: str = "draft", dry_run: bool = False) -> dict:
    evt = normalize_event(raw_evt)
    product = (evt.get("product") or "").lower()
    event = (evt.get("event") or "post_created").lower()
    post_id = _post_id(evt)
    event_id = str(evt.get("event_id") or (f"RFX-POST-{post_id}" if post_id is not None else ""))
    result = {"event_id": event_id, "post_id": post_id, "status": "skipped"}

    if product and product != TIER_ID:
        result["reason"] = f"product={product!r}"
        return result
    allowed = {s.strip() for s in (tier.raw.get("POST_ON_EVENTS") or "").split(",") if s.strip()}
    if allowed and event not in allowed:
        result["reason"] = f"event {event!r} not in POST_ON_EVENTS {sorted(allowed)}"
        return result
    if post_id is None:
        result["status"] = "error"
        result["reason"] = "no post_id"
        return result

    # Stale-message guard — only when the event carries created_at (an inline
    # --event '{"post_id":N}' has none, which is how you push an old post by hand).
    max_age = _int_knob(tier, "REFLEX_MAX_AGE_MINUTES")
    if max_age and evt.get("created_at"):
        try:
            created = datetime.fromisoformat(str(evt["created_at"]).replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - created
            if age > timedelta(minutes=max_age):
                result["reason"] = (f"stale: created {age.total_seconds() / 60:.0f} min ago, "
                                    f"floor is {max_age} (REFLEX_MAX_AGE_MINUTES)")
                return result
        except ValueError:
            pass

    if dedupe.seen(event_id):
        result["status"] = "duplicate"
        return result

    capped = cap_reason(tier, dedupe)
    if capped and not dry_run:
        result["reason"] = capped
        return result

    db = db or ReflexPostsDB(table=tier.raw.get("DATA_SOURCE_1_TABLE") or "public.best_signal_posts")
    if not db.enabled:
        result["status"] = "error"
        result["reason"] = "REFLEX_SUPABASE_DB_URL unset"
        return result

    iids = integration_ids_for(tier)
    labels = [channel_label(tier, i) for i in iids]
    result["channels"] = labels

    if dry_run:
        try:
            row = db.peek(post_id)
        except Exception as e:  # noqa: BLE001
            result.update(status="error", reason=f"db: {str(e)[:200]}")
            return result
        if not row:
            result["reason"] = "no unposted row (already posted or missing)"
            return result
        png = _write_png(event_id, row.get("image_png"))
        # Preview both standard channels even before any account is connected.
        preview = labels or ["X", "LinkedIn"]
        result.update(status="dry-run", media=str(png) if png else None,
                      would_skip=quality_gate(tier, row) or capped,
                      alt=alt_text(row),
                      rendered={lbl: with_cta(tier, lbl, compose_for_channel(tier, lbl, row),
                                               premium_line(tier, row))
                                for lbl in preview},
                      x_len=x_len(compose_for_channel(tier, "X", row)))
        return result

    if not iids:
        result["status"] = "no-channels"
        result["reason"] = "integration_ids_for(reflex) is empty (X id set? LINKEDIN_ENABLED?)"
        return result

    try:
        with db.claim(post_id) as (row, mark_posted):
            if not row:
                result["reason"] = "no unposted row (already posted, missing, or held by another worker)"
                return result
            gated = quality_gate(tier, row)
            if gated:
                # Stays posted=false: the weekly recap still counts it.
                result["reason"] = gated
                return result
            result.update(_publish(
                tier, iids, source_id=event_id, png=row.get("image_png"), alt=alt_text(row),
                text_for=lambda lbl: compose_for_channel(tier, lbl, row),
                extra=premium_line(tier, row), mode=mode))
            if result["status"] == "posted":
                dedupe_meta = {"post_id": post_id, "mode": mode}
                if mode == "draft":
                    # A draft isn't a publish: leave posted=false and no dedupe
                    # row, so the real (now) run still goes out.
                    _log("draft_row_left_unposted", event_id=event_id)
                else:
                    dedupe.mark(event_id, dedupe_meta)
                    dedupe.bump()
                    mark_posted()
                    _log("row_marked_posted", event_id=event_id, post_id=post_id)
    except Exception as e:  # noqa: BLE001
        # DB trouble after a successful publish: the dedupe row (written before
        # the commit) still blocks a repost.
        if result["status"] != "posted":
            result["status"] = "error"
        result["reason"] = f"db: {str(e)[:200]}"
    return result


def _publish(tier, iids: list[str], *, source_id: str, png: bytes | None, alt: str,
             text_for, mode: str, source_type: str = "reflex_post",
             extra: str | None = None) -> dict:
    """Upload the image once, then post text_for(label) (+ CTA) per channel."""
    event_id = source_id
    png = _write_png(source_id, png)
    client = PostizClient(api_key=os.environ.get("POSTIZ_API_KEY"))
    media: list[dict] = []
    if png:
        try:
            up = _as_obj(client.upload(png))
            if up.get("id") and up.get("path"):
                media.append({"id": up["id"], "path": up["path"], "alt": alt})
        except Exception as e:  # noqa: BLE001
            _log("upload_failed", media=str(png), error=str(e)[:300])
    if not media:
        # The image IS the post — never publish Reflex text-only.
        return {"status": "error", "reason": "image missing or upload failed"}

    posted_channels = []
    texts = {}
    for iid in iids:
        lbl = channel_label(tier, iid)
        parts = with_cta(tier, lbl, text_for(lbl), extra)
        texts[lbl] = "\n\n".join(parts)
        try:
            resp = client.create_post(parts=parts, integration_ids=[iid],
                                      mode=mode, media=media)
            pid = _as_obj(resp).get("id") or _as_obj(resp).get("postId")
            posted_channels.append({"channel": lbl,
                                    "state": "PUBLISHED" if mode == "now" else "DRAFT",
                                    "post_id": pid})
            _log("posted", event_id=event_id, channel=lbl, mode=mode, post_id=pid)
        except Exception as e:  # noqa: BLE001
            posted_channels.append({"channel": lbl, "state": "ERROR", "error": str(e)[:300]})
            _log("post_failed", event_id=event_id, channel=lbl, error=str(e)[:300])

    ok = [c for c in posted_channels if c["state"] in ("PUBLISHED", "DRAFT")]
    if ok:
        posted_log.mark_posted(source_type=source_type, source_id=event_id, tier=TIER_ID,
                               mode=mode, text=texts.get("X") or next(iter(texts.values())),
                               integration_ids=list(iids),
                               response={"channels": posted_channels})
    return {"status": "posted" if ok else "error", "rendered": texts,
            "result_channels": posted_channels}


def run_recap(tier, dedupe: Dedupe, *, db: ReflexPostsDB | None = None,
              mode: str = "draft", dry_run: bool = False) -> dict:
    """Weekly scorecard post (Cloud Scheduler -> POST /recap, Fri after close).
    Counts every post Reflex made in the last REFLEX_RECAP_DAYS, attaches the
    biggest move's image, and never touches the DB. Once per ISO week."""
    key = f"RFX-RECAP-{dedupe.periods()['week'].split(':', 1)[1]}"
    result = {"event_id": key, "status": "skipped"}
    if dedupe.seen(key) and not dry_run:
        result["status"] = "duplicate"
        return result
    db = db or ReflexPostsDB(table=tier.raw.get("DATA_SOURCE_1_TABLE") or "public.best_signal_posts")
    if not db.enabled:
        result.update(status="error", reason="REFLEX_SUPABASE_DB_URL unset")
        return result
    try:
        rows = db.since(_int_knob(tier, "REFLEX_RECAP_DAYS") or 7)
        text = compose_recap(tier, rows)
        best = db.peek(int(rows[0]["id"]), include_posted=True) if (text and rows) else None
    except Exception as e:  # noqa: BLE001
        result.update(status="error", reason=f"db: {str(e)[:200]}")
        return result
    result["posts_in_window"] = len(rows)
    if not text:
        result["reason"] = f"only {len(rows)} post(s) this week (REFLEX_RECAP_MIN_POSTS)"
        return result
    alt = alt_text(best) if best else ""
    iids = integration_ids_for(tier)
    if dry_run:
        png = _write_png(key, (best or {}).get("image_png"))
        result.update(status="dry-run", media=str(png) if png else None, x_len=x_len(text),
                      rendered={lbl: with_cta(tier, lbl, text)
                                for lbl in ([channel_label(tier, i) for i in iids] or ["X", "LinkedIn"])})
        return result
    if not iids:
        result.update(status="no-channels", reason="integration_ids_for(reflex) is empty")
        return result
    result.update(_publish(tier, iids, source_id=key, png=(best or {}).get("image_png"),
                           alt=alt, text_for=lambda _lbl: text, mode=mode,
                           source_type="reflex_recap"))
    if result["status"] == "posted" and mode != "draft":
        dedupe.mark(key, {"mode": mode, "posts": len(rows)})
    return result


# ----------------------------------------------------------------------- modes
def run_pull(tier, dedupe, *, mode, dry_run, max_msgs, timeout, sub=None):
    """Drain the subscription once (local / catch-up). An empty subscription
    surfaces as DeadlineExceeded = "nothing to pull", exit 0. Every message is
    ACKED, errors included — same never-retry rule as the push handler."""
    sub = (sub or tier.raw.get("REFLEX_PUBSUB_SUBSCRIPTION")
           or os.getenv("REFLEX_PUBSUB_SUBSCRIPTION") or "").strip()
    project = (tier.raw.get("REFLEX_PUBSUB_PROJECT") or os.getenv("GCP_PROJECT") or "").strip()
    if not sub or not project:
        _log("pull_misconfigured", subscription=sub, project=project)
        return 2

    from google.api_core import exceptions as gexc
    from google.cloud import pubsub_v1

    client = pubsub_v1.SubscriberClient()
    path = client.subscription_path(project, sub)
    _log("pull_start", subscription=path, max=max_msgs, budget_s=timeout)

    got = errors = 0
    deadline = time.time() + timeout
    while got < max_msgs and time.time() < deadline:
        rpc_timeout = max(2.0, min(10.0, deadline - time.time()))
        try:
            resp = client.pull(
                request={"subscription": path,
                         "max_messages": min(10, max_msgs - got)},
                timeout=rpc_timeout,
                retry=None,
            )
        except gexc.NotFound:
            _log("pull_no_such_subscription", subscription=path)
            return 2
        except (gexc.DeadlineExceeded, gexc.RetryError, gexc.ServiceUnavailable):
            break                    # empty / quiet subscription — normal
        except Exception as e:       # noqa: BLE001
            _log("pull_rpc_error", error=str(e)[:300])
            return 3

        if not resp.received_messages:
            break

        ack = []
        for rm in resp.received_messages:
            env = {"message": {"data": base64.b64encode(rm.message.data).decode(),
                               "attributes": dict(rm.message.attributes),
                               "messageId": rm.message.message_id}}
            r = process_event(env, tier=tier, dedupe=dedupe, mode=mode, dry_run=dry_run)
            _log("processed", **{k: v for k, v in r.items() if k != "rendered"})
            got += 1
            if r.get("status") == "error":
                errors += 1
            ack.append(rm.ack_id)
        if ack:
            client.acknowledge(request={"subscription": path, "ack_ids": ack})

    _log("pull_done", processed=got, errors=errors)
    return 1 if errors else 0


def build_app(tier, dedupe, *, mode, dry_run):
    """Flask app for Pub/Sub PUSH delivery (gunicorn create_app / --serve).
    ALWAYS acks (204): the outcome of a delivered message is final — a retry
    can't fix a provider-side rejection, it only multiplies duplicate posts.
    An unposted row stays posted=false, so `make reflex-event` can re-fire it."""
    from flask import Flask, request
    app = Flask(__name__)

    @app.get("/healthz")
    def health():  # noqa: ANN202
        return "ok", 200

    @app.post("/")
    def push():  # noqa: ANN202
        try:
            r = process_event(request.get_json(force=True, silent=True) or {},
                              tier=tier, dedupe=dedupe, mode=mode, dry_run=dry_run)
            _log("processed", **r)
        except Exception as e:  # noqa: BLE001 — a crash here must still ack
            _log("push_handler_crash", error=str(e)[:400])
        return "", 204

    @app.post("/recap")
    def recap():  # noqa: ANN202 — Cloud Scheduler (OIDC as facades-poster-sa)
        try:
            r = run_recap(tier, dedupe, mode=mode, dry_run=dry_run)
            _log("recap", **{k: v for k, v in r.items() if k != "rendered"})
        except Exception as e:  # noqa: BLE001 — never make Scheduler retry a post
            _log("recap_crash", error=str(e)[:400])
        return "", 204

    return app


def create_app():
    """gunicorn app factory:  gunicorn 'bin.reflex_poster:create_app()'
    Config from the environment (Cloud Run --set-env-vars / --set-secrets):
    REFLEX_POSTER_MODE (default 'now'), REFLEX_POSTER_DRY_RUN."""
    load_dotenv()
    tier = load_tier(TIER_ID)
    dedupe = Dedupe(tier)
    mode = os.environ.get("REFLEX_POSTER_MODE", "now")
    dry_run = os.environ.get("REFLEX_POSTER_DRY_RUN", "").lower() in ("1", "true", "yes")
    _log("serve_start", entrypoint="gunicorn", mode=mode, dry_run=dry_run)
    return build_app(tier, dedupe, mode=mode, dry_run=dry_run)


def run_serve(tier, dedupe, *, mode, dry_run, port):
    _log("serve_start", entrypoint="flask", port=port, mode=mode, dry_run=dry_run)
    build_app(tier, dedupe, mode=mode, dry_run=dry_run).run(host="0.0.0.0", port=port)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--serve", action="store_true", help="HTTP server for Pub/Sub push (Cloud Run)")
    g.add_argument("--pull", action="store_true", help="pull-drain the subscription once")
    g.add_argument("--event", help="process one inline event (JSON string or @file), "
                   "e.g. '{\"post_id\":5}'")
    g.add_argument("--recap", action="store_true", help="post the weekly scorecard now")
    ap.add_argument("--mode", choices=["draft", "now"], default="draft",
                    help="Postiz post mode (default: draft — safe, row stays unposted)")
    ap.add_argument("--dry-run", action="store_true",
                    help="read the row read-only, write PNG + text locally, never post")
    ap.add_argument("--max", type=int, default=50, help="--pull: max messages")
    ap.add_argument("--timeout", type=int, default=30, help="--pull: seconds")
    ap.add_argument("--sub", help="--pull: subscription name override "
                    "(else REFLEX_PUBSUB_SUBSCRIPTION / tier.config)")
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    args = ap.parse_args()

    load_dotenv()
    tier = load_tier(TIER_ID)
    dedupe = Dedupe(tier)

    if args.event:
        payload = args.event
        if payload.startswith("@"):
            payload = Path(payload[1:]).read_text()
        r = process_event(json.loads(payload), tier=tier, dedupe=dedupe,
                          mode=args.mode, dry_run=args.dry_run)
        print(json.dumps(r, indent=2, default=str, ensure_ascii=False))
        return 0 if r.get("status") in ("posted", "dry-run", "duplicate", "skipped") else 1

    if args.recap:
        r = run_recap(tier, dedupe, mode=args.mode, dry_run=args.dry_run)
        print(json.dumps(r, indent=2, default=str, ensure_ascii=False))
        return 0 if r.get("status") != "error" else 1

    if args.pull:
        return run_pull(tier, dedupe, mode=args.mode, dry_run=args.dry_run,
                        max_msgs=args.max, timeout=args.timeout, sub=args.sub)

    run_serve(tier, dedupe, mode=args.mode, dry_run=args.dry_run, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
