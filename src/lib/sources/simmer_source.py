"""Source adapter for Simmer (Facades) — EdgeLane's credit-spread watchdog.

Unlike the KG sources (a curated cards.json / Firestore), Simmer is event-driven:
the engine publishes ticker state-change events to Pub/Sub and `bin/simmer_poster.py`
consumes them. This adapter is the *read side* — pull a ticker's current state
(or the full expanded card) on demand from EdgeLane's read-only API:

  GET  {base}/simmer/ready?since=YYYY-MM-DD          -> [{symbol, expiry, ...}]
  GET  {base}/simmer/state/<SYMBOL>?block=<block>    -> one block or the full card
       block ∈ card | score | gates | sentiment | evolution

Auth: a service token (bearer), env name from DATA_SOURCE_*_TOKEN_ENV. If the
base URL is a *.run.app host and no token is set, falls back to a Google OIDC
identity token minted from GOOGLE_APPLICATION_CREDENTIALS (same pattern as
src/lib/handles.py).

Card ids are synthetic and reversible: `SMR-<SYMBOL>-<computed_date>-<expiry>`
(dates YYMMDD). Used by `factory.build_source` for DATA_SOURCE_*_TYPE="simmer_api".
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .base import Source

_ID_TOKEN_CACHE: dict = {}  # audience -> (token, cached_at_epoch)

BLOCKS = ("card", "score", "gates", "sentiment", "evolution")


def _oidc_token(audience: str) -> str | None:
    """OIDC token for a private *.run.app endpoint, or None. Cached ~50 min."""
    hit = _ID_TOKEN_CACHE.get(audience)
    if hit and time.time() - hit[1] < 3000:
        return hit[0]
    try:
        key = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not key or not os.path.isfile(key):
            return None
        from google.oauth2 import service_account
        import google.auth.transport.requests as gar
        creds = service_account.IDTokenCredentials.from_service_account_file(
            key, target_audience=audience)
        creds.refresh(gar.Request())
        _ID_TOKEN_CACHE[audience] = (creds.token, time.time())
        return creds.token
    except Exception:  # noqa: BLE001 — no creds/lib/network -> unauthenticated attempt
        return None


def _yymmdd(s: str) -> str:
    """'2026-08-28' or ISO ts -> '260828'. '' on anything unparseable."""
    if not s:
        return ""
    s = str(s)[:10]
    try:
        return datetime.fromisoformat(s).strftime("%y%m%d")
    except ValueError:
        return s.replace("-", "")[-6:]


def make_card_id(symbol: str, computed_at: str, expiry: str) -> str:
    return f"SMR-{(symbol or '').upper()}-{_yymmdd(computed_at)}-{_yymmdd(expiry)}"


def parse_card_id(card_id: str) -> tuple[str, str, str]:
    """'SMR-MSTR-260821-260828' -> ('MSTR', '260821', '260828'). Symbol only if short."""
    parts = (card_id or "").split("-")
    if len(parts) >= 4 and parts[0] == "SMR":
        return parts[1].upper(), parts[2], parts[3]
    if len(parts) >= 2 and parts[0] == "SMR":
        return parts[1].upper(), "", ""
    return card_id.upper(), "", ""


class SimmerAPI(Source):
    def __init__(self, base_url: str, token_env: str = "SIMMER_API_TOKEN",
                 ready_path: str = "/simmer/ready",
                 state_path: str = "/simmer/state", **_):
        self.base = (base_url or "").rstrip("/")
        self.token = os.environ.get(token_env or "SIMMER_API_TOKEN", "").strip()
        self.ready_path = ready_path or "/simmer/ready"
        self.state_path = (state_path or "/simmer/state").rstrip("/")
        if not self.base:
            raise ValueError("simmer_api source: DATA_SOURCE_*_BASE_URL is empty "
                             "(set SIMMER_API_BASE in .env)")

    # ---- HTTP -----------------------------------------------------------
    def _headers(self) -> dict:
        h = {"User-Agent": "simmer-poster/1.0", "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        elif ".run.app" in self.base:
            p = urllib.parse.urlsplit(self.base)
            tok = _oidc_token(f"{p.scheme}://{p.netloc}")
            if tok:
                h["Authorization"] = f"Bearer {tok}"
        return h

    def _get(self, path: str, params: dict | None = None, timeout: int = 20) -> dict | list:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url, method="GET", headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))

    # ---- Source interface --------------------------------------------------
    def list_recent(self, since: datetime, limit: int = 50, **filters) -> list[dict]:
        since_str = since.astimezone(timezone.utc).strftime("%Y-%m-%d")
        try:
            rows = self._get(self.ready_path, {"since": since_str, "limit": limit})
        except Exception:  # noqa: BLE001 — feed down: nothing ready this pass, never crash the poller
            return []
        if isinstance(rows, dict):
            rows = rows.get("ready") or rows.get("items") or []
        out = [self._to_card(r, state="ready") for r in rows if isinstance(r, dict)]
        out.sort(key=lambda c: c.get("date") or "", reverse=True)
        return out[:limit]

    def get(self, item_id: str) -> dict:
        symbol, _, expiry_yy = parse_card_id(item_id)
        try:
            raw = self._get(f"{self.state_path}/{urllib.parse.quote(symbol)}", {"block": "card"})
        except urllib.error.HTTPError as e:
            # 404 "no readiness for <SYM>" — event fired before the engine stored
            # a card, or a synthetic fire. Caller falls back to a minimal card.
            raise KeyError(f"simmer_api: {symbol} not available (HTTP {e.code})") from e
        except urllib.error.URLError as e:
            raise KeyError(f"simmer_api: {symbol} unreachable ({e.reason})") from e
        if not isinstance(raw, dict) or not raw:
            raise KeyError(f"simmer_api: no state for '{symbol}' ({item_id})")
        card = self._to_card(raw, state=raw.get("state"))
        # Keep the id the caller asked for so posted_log / dedupe stay stable.
        card["card_id"] = item_id
        card["_requested_expiry"] = expiry_yy
        return card

    def get_related(self, item_id: str) -> list[dict]:
        return []

    def state_block(self, symbol: str, block: str) -> dict:
        """On-demand single block for a symbol. block ∈ BLOCKS."""
        if block not in BLOCKS:
            raise ValueError(f"block must be one of {BLOCKS}, got {block!r}")
        r = self._get(f"{self.state_path}/{urllib.parse.quote(symbol.upper())}", {"block": block})
        return r if isinstance(r, dict) else {"value": r}

    # ---- mapping --------------------------------------------------------
    @staticmethod
    def _to_card(raw: dict, state: str | None = None) -> dict:
        """EdgeLane engine output (flat, or {env:{...}}) -> the card dict the
        recipes/imagery layers expect."""
        env = raw.get("env") if isinstance(raw.get("env"), dict) else raw
        sym = str(env.get("symbol") or raw.get("symbol") or "").upper()
        expiry = env.get("expiration") or raw.get("expiry") or raw.get("expiration") or ""
        computed = env.get("computed_at") or raw.get("computed_at") or ""
        metrics = env.get("metrics") or {}
        dq = env.get("data_quality") or {}
        veto = list(env.get("veto_reasons") or [])
        decision = env.get("decision") or raw.get("decision") or ""
        st = (state or raw.get("state")
              or ("ready" if decision in ("ok", "ready", "pass") else "watch_entered"))
        sentiment = raw.get("sentiment") or env.get("sentiment") or {
            "news_quality": dq.get("news"),
        }
        card_id = make_card_id(sym, computed, expiry)
        return {
            "card_id": card_id,
            "id": card_id,
            "symbol": sym,
            "state": st,
            "decision": decision,
            "expiry": str(expiry)[:10],
            "dte": env.get("dte") or raw.get("dte"),
            "spot": env.get("spot"),
            "score": env.get("score"),
            "confidence": env.get("confidence"),
            "regime": (env.get("regime") or {}).get("state"),
            "structure": env.get("structure"),
            "strikes": env.get("strikes"),
            "credit_mid": env.get("credit_mid"),
            "gates": {
                "veto_reasons": veto,
                "failed": veto,
                "clear": not veto and decision in ("ok", "ready", "pass"),
            },
            "sentiment": sentiment,
            "metrics": {
                "iv_pct": metrics.get("iv_percentile_effective") or metrics.get("iv_percentile"),
                "vrp": metrics.get("vrp"),
                "em_1sd": metrics.get("em_1sd"),
                "em_2sd": metrics.get("em_2sd"),
                "max_pain": metrics.get("max_pain"),
                "rr25": metrics.get("rr25_put_minus_call"),
                "term_slope": metrics.get("term_slope"),
            },
            "data_quality": dq,
            "date": str(computed)[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "computed_at": computed,
            "headline": f"${sym} — {'ready to serve' if st == 'ready' else 'started simmering'}",
            "entities": [{
                "name": sym,
                "x_handle": f"${sym}" if sym else None,
                "linkedin_handle": None,
            }],
            "url": f"https://simmer.facades.trade/?symbol={sym}",
        }
