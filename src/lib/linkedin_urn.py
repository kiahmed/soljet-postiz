"""Resolve a LinkedIn vanity slug → organization URN, for REAL @mentions.

LinkedIn's post API only renders a mention — links the account, notifies it,
counts as an actual tag — when the commentary carries the token
`@[Name](urn:li:organization:<id>)`. A bare `@slug` is dead text (this is the
whole bug: Postiz's `fixText` matches exactly that token and leaves everything
else as literal text).

Division of labour (see docs/handle-resolution-spec.md):
  - The KG answers the HARD question "which company owns this vanity name"
    (needs graph/sector context; impossible at post time) and hands us the
    verified slug, e.g. "@spiritai-robotics".
  - We turn slug → URN here via LinkedIn's authenticated vanityName finder,
    using the app token Postiz already holds. Deterministic, cacheable, no
    scraping wall.

Fail closed: any miss (wrong/stale slug, throttle, no token) returns None and
the caller emits NO tag — never a broken mention. Only LinkedIn needs this; X's
`@handle` already works as plain text.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_PATH = REPO_ROOT / "data" / "linkedin_urn_cache.json"
_NEG_TTL = 3 * 24 * 3600          # re-try a missed slug after 3 days
_API = "https://api.linkedin.com/rest/organizations"
_LI_VERSION = "202601"            # matches Postiz's LinkedIn-Version header

_token_cache: str | None = None


# --------------------------------------------------------------------------- token
def _read_token() -> str | None:
    """The LinkedIn app token. Prefer an explicit env var (scheduler passes it);
    otherwise read the LIVE token straight from Postiz's Postgres — Postiz owns
    the OAuth refresh, so reading it each run always gets the current one."""
    global _token_cache
    if _token_cache:
        return _token_cache
    tok = os.getenv("LINKEDIN_ACCESS_TOKEN")
    if tok:
        _token_cache = tok.strip()
        return _token_cache
    container = os.getenv("POSTIZ_PG_CONTAINER", "postiz-postgres")
    user = os.getenv("POSTIZ_PG_USER", "postiz-user")
    db = os.getenv("POSTIZ_PG_DB", "postiz-db-local")
    provider = os.getenv("LINKEDIN_PROVIDER_ID", "linkedin-page")
    try:
        out = subprocess.run(
            ["docker", "exec", container, "psql", "-U", user, "-d", db,
             "-t", "-A", "-c",
             "select token from \"Integration\" where "
             f"\"providerIdentifier\"='{provider}' order by \"tokenExpiration\" "
             "desc nulls last limit 1;"],
            capture_output=True, text=True, timeout=15)
        tok = (out.stdout or "").strip()
        if tok:
            _token_cache = tok
            return tok
    except Exception:  # noqa: BLE001 — docker/psql absent or down → fail closed
        pass
    return None


# --------------------------------------------------------------------------- cache
def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(c: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(c, indent=2, sort_keys=True))
    except Exception:  # noqa: BLE001 — cache is best-effort
        pass


# --------------------------------------------------------------------------- lookup
def _slug(handle: str) -> str:
    """'@spiritai-robotics' or a full company URL → 'spiritai-robotics'."""
    s = handle.strip()
    if "linkedin.com/company/" in s:
        s = s.split("linkedin.com/company/", 1)[1]
    return s.lstrip("@").strip("/").split("/")[0].split("?")[0]


def _finder(slug: str, token: str) -> dict | None:
    """LinkedIn vanityName finder → {'id','name'} for the first match, or None."""
    url = f"{_API}?q=vanityName&vanityName={urllib.parse.quote(slug)}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": _LI_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001 — 4xx/5xx/throttle/network → fail closed
        return None
    els = data.get("elements") or []
    if not els:
        return None
    e = els[0]
    oid = e.get("id")
    if not oid:
        return None
    return {"id": str(oid), "name": e.get("localizedName") or ""}


def resolve(handle: str) -> dict | None:
    """slug/handle/URL → {'id','name'} or None. Cached (positive forever,
    negative for _NEG_TTL). Never raises."""
    if not handle:
        return None
    slug = _slug(handle)
    if not slug:
        return None
    cache = _load_cache()
    hit = cache.get(slug)
    now = time.time()
    if hit:
        if hit.get("id"):
            return {"id": hit["id"], "name": hit.get("name", "")}
        if now - hit.get("ts", 0) < _NEG_TTL:
            return None  # cached miss, still fresh
    token = _read_token()
    if not token:
        return None  # no token → fail closed, do NOT cache (transient)
    res = _finder(slug, token)
    cache[slug] = ({"id": res["id"], "name": res["name"], "ts": now}
                   if res else {"id": None, "ts": now})
    _save_cache(cache)
    return res


def to_mention(handle: str, fallback_name: str | None = None) -> str | None:
    """slug/handle → '@[Name](urn:li:organization:<id>)' (the ONLY form LinkedIn
    renders as a real mention), or None to abstain. Uses the org's official
    localizedName; falls back to the entity name only if the API omits it."""
    res = resolve(handle)
    if not res:
        return None
    name = (res.get("name") or fallback_name or _slug(handle)).strip()
    return f"@[{name}](urn:li:organization:{res['id']})"
