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

    # ---- mapping (provisional — see module docstring) -------------------
    @staticmethod
    def _to_card(raw: dict) -> dict:
        """EdgeLane's future Matrix engine output -> the card dict recipes.py
        expects. Field names guessed from the two UI screenshots this spec was
        built from (strategy name, composite, tags, Net/MaxP/MaxL/POP/EV) —
        reconcile against the real payload once /matrix/state/<SYM> exists."""
        env = raw.get("env") if isinstance(raw.get("env"), dict) else raw
        sym = str(env.get("symbol") or raw.get("symbol") or "").upper()
        computed = env.get("computed_at") or raw.get("computed_at") or ""
        pick = env.get("pick") or raw.get("pick") or {}
        card_id = make_card_id(sym, computed)
        return {
            "card_id": card_id,
            "id": card_id,
            "symbol": sym,
            "strategy": pick.get("strategy") or pick.get("name"),
            "composite": pick.get("composite"),
            "tags": list(pick.get("tags") or []),
            "net_prem": pick.get("net_prem"),
            "max_p": pick.get("max_p"),
            "max_l": pick.get("max_l"),
            "pop": pick.get("pop"),
            "ev": pick.get("ev"),
            "hint_text": pick.get("hint_text") or raw.get("hint_text"),
            "date": str(computed)[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "computed_at": computed,
            "headline": f"${sym} — engine pick: {pick.get('strategy') or pick.get('name') or ''}".strip(),
            "entities": [{
                "name": sym,
                "x_handle": f"${sym}" if sym else None,
                "linkedin_handle": None,
            }],
            "url": f"https://matrix.facades.trade/?symbol={sym}",
        }
