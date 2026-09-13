"""Source adapter for Matrix (Facades) — EdgeLane's strategy-grid engine.

Same shape as simmer_source.py's SimmerAPI (event-driven; this adapter is the
*read side*), generalized for Matrix's multiple post moments instead of one:

  GET  {base}/matrix/state/<SYMBOL>?block=<block>    -> one block or the full card
       block ∈ pick | grid | bias | win_eval | walls

Auth: a service token (bearer), env name from DATA_SOURCE_*_TOKEN_ENV — the
same OIDC-fallback pattern as simmer_source.py for a *.run.app host with no
token set.

**As of this writing EdgeLane has not built this endpoint** (see
docs/matrix_integration.md / EdgeLane's docs/matrix_events_update.md) — every
`get()` call will fail (connection error or 404) and the caller
(recipes.py::recipe_matrix) falls back to a minimal card built from the
Pub/Sub event's own attributes, exactly like Simmer did before its API
existed. `_to_card`'s field mapping below is therefore provisional: reconcile
it against the real response shape once EdgeLane ships `/matrix/state/<SYM>`.

Card ids are synthetic and reversible: `MTX-<SYMBOL>-<computed_date>`.
Used by `factory.build_source` for DATA_SOURCE_*_TYPE="matrix_api".
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .base import Source
from .simmer_source import _oidc_token  # same OIDC-fallback helper, shared

BLOCKS = ("pick", "grid", "bias", "win_eval", "walls")

# Which named snap `view` best illustrates each post moment — read by
# imagery.py::_matrix_snap and recipes.py::_matrix_bundle.
STATE_VIEW = {
    "pick_selected": "engine_pick",
    "daily_recap": "engine_pick",
    "bias_aligned": "bias_chip",
    "bias_diverged": "bias_chip",
    "win_rate_notable": "win_eval_grid",
    "session_open": "walls_chip",
    "grid_digest": "strategy_grid",
}


def _yymmdd(s: str) -> str:
    if not s:
        return ""
    s = str(s)[:10]
    try:
        return datetime.fromisoformat(s).strftime("%y%m%d")
    except ValueError:
        return s.replace("-", "")[-6:]


def make_card_id(symbol: str, computed_at: str) -> str:
    return f"MTX-{(symbol or '').upper()}-{_yymmdd(computed_at)}"


def parse_card_id(card_id: str) -> tuple[str, str]:
    """'MTX-NVDA-260913' -> ('NVDA', '260913')."""
    parts = (card_id or "").split("-")
    if len(parts) >= 3 and parts[0] == "MTX":
        return parts[1].upper(), parts[2]
    if len(parts) >= 2 and parts[0] == "MTX":
        return parts[1].upper(), ""
    return card_id.upper(), ""


class MatrixAPI(Source):
    def __init__(self, base_url: str, token_env: str = "MATRIX_API_TOKEN",
                 state_path: str = "/matrix/state", **_):
        self.base = (base_url or "").rstrip("/")
        self.token = os.environ.get(token_env or "MATRIX_API_TOKEN", "").strip()
        self.state_path = (state_path or "/matrix/state").rstrip("/")
        if not self.base:
            raise ValueError("matrix_api source: DATA_SOURCE_*_BASE_URL is empty "
                             "(set MATRIX_API_BASE in .env)")

    # ---- HTTP -----------------------------------------------------------
    def _headers(self) -> dict:
        h = {"User-Agent": "matrix-poster/1.0", "Accept": "application/json"}
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
        # Matrix has no "ready list" endpoint (unlike Simmer's /simmer/ready) —
        # its post moments are event-driven only. Kept for Source-interface
        # parity; never crashes the poller.
        return []

    def get(self, item_id: str) -> dict:
        symbol, _ = parse_card_id(item_id)
        try:
            raw = self._get(f"{self.state_path}/{urllib.parse.quote(symbol)}", {"block": "pick"})
        except urllib.error.HTTPError as e:
            raise KeyError(f"matrix_api: {symbol} not available (HTTP {e.code})") from e
        except urllib.error.URLError as e:
            raise KeyError(f"matrix_api: {symbol} unreachable ({e.reason})") from e
        if not isinstance(raw, dict) or not raw:
            raise KeyError(f"matrix_api: no state for '{symbol}' ({item_id})")
        card = self._to_card(raw)
        # All 6 post moments need data beyond "pick" (bias/grid/win_eval/walls)
        # — fetch every block best-effort and stash it on the card, so
        # compose_matrix() can serve any state from one get() regardless of
        # which one the event actually asked for. A block hiccup never blocks
        # the others (each is its own try/except, card[key] just ends up {}).
        for block in ("bias", "grid", "win_eval", "walls"):
            try:
                r = self._get(f"{self.state_path}/{urllib.parse.quote(symbol)}", {"block": block})
                block_data = (r or {}).get("data") or {}
                # "grid" nests the actual 8-strategy dict one level further —
                # {"expiration":..., "grid": {bull_put:{...}, ...}} — every
                # other block's useful content is directly under "data".
                card[block] = block_data.get("grid") or {} if block == "grid" else block_data
            except Exception:  # noqa: BLE001
                card[block] = {}
        trust = (card.get("bias") or {}).get("trust") or {}
        card["hint_text"] = trust.get("hint_text")
        card["bias_trust_state"] = trust.get("state")
        card["win_rate"] = trust.get("win_rate")
        card["graded"] = trust.get("graded")
        card["card_id"] = item_id
        return card

    def get_related(self, item_id: str) -> list[dict]:
        return []

    def state_block(self, symbol: str, block: str) -> dict:
        """On-demand single block for a symbol. block ∈ BLOCKS."""
        if block not in BLOCKS:
            raise ValueError(f"block must be one of {BLOCKS}, got {block!r}")
        r = self._get(f"{self.state_path}/{urllib.parse.quote(symbol.upper())}", {"block": block})
        return r if isinstance(r, dict) else {"value": r}

    # ---- mapping ----------------------------------------------------------
    @staticmethod
    def _to_card(raw: dict) -> dict:
        """EdgeLane's real `?block=pick` response -> the card dict recipes.py
        expects. Confirmed against the live endpoint 2026-09-13:

            {"symbol": "SPX", "block": "pick", "data": {
                "expiration": "2026-09-14", "spot": 7656.98,
                "pick": {"strategy": "bear_call", "name": "Bear Call Spread",
                         "short": "Bear Call", "composite_score": 86.1,
                         "composite_verdict": {"label": "tradeable on limit", ...},
                         "structure_text": "Short 7680.0C / Long 7720.0C",
                         "net_premium": 7.625, "max_profit": 7.625,
                         "max_loss": 32.375, "pop_pct": 68.5, "ev": -4.97,
                         "health": "healthy", "liquidity": "high", ...}}}

        There is no `tags` array — `health`/`liquidity`/`composite_verdict.label`
        are reconstructed into the same HEALTHY / LIQ HIGH / TRADEABLE ON LIMIT
        vocabulary compose_matrix()'s tag-clause lookup already expects, so no
        change was needed there. `hint_text` isn't in this block — get() fills
        it in separately from the `bias` block."""
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        sym = str(raw.get("symbol") or data.get("symbol") or "").upper()
        pick = data.get("pick") or {}
        verdict = pick.get("composite_verdict") or {}
        tags = []
        if pick.get("health"):
            tags.append(str(pick["health"]).upper())
        if pick.get("liquidity"):
            tags.append(f"LIQ {str(pick['liquidity']).upper()}")
        if verdict.get("label"):
            tags.append(str(verdict["label"]).upper())
        return {
            "card_id": None,   # get() overwrites with the caller's item_id
            "id": None,
            "symbol": sym,
            "strategy": pick.get("short") or pick.get("name") or pick.get("strategy"),
            "composite": pick.get("composite_score"),
            "tags": tags,
            "net_prem": pick.get("net_premium"),
            "max_p": pick.get("max_profit"),
            "max_l": pick.get("max_loss"),
            "pop": pick.get("pop_pct"),
            "ev": pick.get("ev"),
            "structure_text": pick.get("structure_text"),
            "expiry": data.get("expiration") or "",
            "hint_text": None,  # filled by get() from the bias block
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "headline": f"${sym} — engine pick: {pick.get('short') or pick.get('name') or ''}".strip(),
            "entities": [{
                "name": sym,
                "x_handle": f"${sym}" if sym else None,
                "linkedin_handle": None,
            }],
            "url": f"https://matrix.facades.trade/?symbol={sym}",
        }
