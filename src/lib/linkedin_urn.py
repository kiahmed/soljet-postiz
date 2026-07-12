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
import re
import subprocess
import sys
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
    desc = e.get("description") or {}
    if isinstance(desc, dict):  # {"localized": {"en_US": "..."}}
        loc = desc.get("localized") or {}
        desc = next(iter(loc.values()), "") if loc else ""
    return {"id": str(oid), "name": e.get("localizedName") or "",
            "desc": desc or ""}


def resolve(handle: str) -> dict | None:
    """slug/handle/URL → {'id','name','desc'} or None. Cached (positive forever,
    negative for _NEG_TTL). Never raises. Pre-'desc' cache rows are refreshed once
    so the collision guard has a description to reason with."""
    if not handle:
        return None
    slug = _slug(handle)
    if not slug:
        return None
    cache = _load_cache()
    hit = cache.get(slug)
    now = time.time()
    if hit:
        if hit.get("id") and "desc" in hit:
            return {"id": hit["id"], "name": hit.get("name", ""),
                    "desc": hit.get("desc", "")}
        if not hit.get("id") and now - hit.get("ts", 0) < _NEG_TTL:
            return None  # cached miss, still fresh
        # positive hit missing 'desc' (old cache) → fall through and refresh
    token = _read_token()
    if not token:
        # no token → fail closed; but honor an old positive hit if present
        if hit and hit.get("id"):
            return {"id": hit["id"], "name": hit.get("name", ""), "desc": ""}
        return None
    res = _finder(slug, token)
    cache[slug] = ({"id": res["id"], "name": res["name"],
                    "desc": res["desc"], "ts": now}
                   if res else {"id": None, "ts": now})
    _save_cache(cache)
    return res


# --------------------------------------------------------------- collision guard
# A slug can resolve to the WRONG same-named org (KG picked "foundation-group-inc"
# — a 501(c)(3) tax-services firm — for a humanoid-robotics "Foundation"). Our
# fail-closed miss guard can't catch that: it DID resolve. So for collision-prone
# names (a single common-word name like "Foundation"/"Figure"/"Bolt"), we verify
# the resolved org's blurb shares real domain terms with the card. No overlap →
# drop. Deterministic, no LLM; better a missing tag than a wrong one.

# corporate suffixes stripped before deciding if a name is a bare common word
_SUFFIXES = {"inc", "llc", "ltd", "corp", "co", "company", "group", "holdings",
             "technologies", "technology", "robotics", "robotic", "ai", "labs",
             "systems", "global", "international", "gmbh", "sa", "plc"}
# generic/marketing/stop tokens that must NOT count as a domain-match
_STOP = {"the", "and", "for", "with", "that", "this", "our", "your", "their",
         "from", "into", "are", "was", "has", "have", "will", "its", "his", "her",
         "company", "provider", "leader", "leading", "trusted", "most", "help",
         "helping", "services", "service", "solutions", "solution", "platform",
         "based", "global", "world", "industry", "professional", "team", "group",
         "mission", "focus", "goal", "growth", "success", "customers", "clients",
         "products", "product", "technology", "technologies", "national", "nation",
         "years", "more", "than", "over", "about", "who", "what", "when", "where"}
# blurb markers of an OFF-domain org (nonprofit/tax/legal/…). Only these, with
# zero positive overlap, condemn a resolved org — so a legit brand whose About
# text simply uses different words than the card (Tesla's EV blurb vs an Optimus
# robot headline) is kept, while a 501(c)(3) tax firm tagged as a robotics co is
# dropped. Positive domain overlap always wins regardless of these.
_OFF_DOMAIN = {"nonprofit", "501", "charity", "charitable", "church", "ministry",
               "diocese", "parish", "gospel", "bookkeeping", "accounting",
               "attorney", "attorneys", "lawyer", "paralegal", "notary",
               "realtor", "realty", "mortgage", "escrow", "staffing", "recruiting",
               "recruitment", "restaurant", "catering", "bakery", "salon", "spa",
               "dental", "dentistry", "orthodontics", "chiropractic", "veterinary",
               "tuition", "kindergarten", "preschool", "daycare", "seminary",
               "funeral", "florist", "plumbing", "landscaping", "boutique",
               "apparel", "cosmetics", "winery", "brewery", "franchise"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
            if len(w) >= 4 and w not in _STOP}


def _norm_name(name: str) -> list[str]:
    toks = [w for w in re.split(r"[^A-Za-z0-9]+", name or "") if w]
    return [w for w in toks if w.lower() not in _SUFFIXES]


def _collision_prone(name: str | None) -> bool:
    """True for a single bare common-word name (collision magnet). False for
    multi-word names (Boston Dynamics), acronyms/all-caps (NVIDIA, UBTECH), or
    names carrying digits — those are distinctive enough to trust."""
    if not name:
        return False
    core = _norm_name(name)
    if len(core) != 1:
        return False
    w = core[0]
    if any(c.isdigit() for c in w):
        return False
    if w.isupper():           # acronym like NVIDIA, ABB, UBTECH
        return False
    return len(w) >= 3


def _context_confidence(org_blurb: str, context: str, exclude: str | None) -> float:
    """Confidence that the resolved org matches what the card is about.

    Positive domain overlap (org About-text ∩ card context, minus the entity's
    own self-matching name) always clears the bar. With ZERO overlap we only
    condemn the org if its blurb carries an off-domain marker (nonprofit/tax/…),
    so a legit brand whose vocabulary just differs from this card is kept."""
    ex = {w.lower() for w in re.split(r"[^A-Za-z0-9]+", exclude or "") if w}
    org_tok = _tokens(org_blurb)
    hits = len((org_tok - ex) & (_tokens(context) - ex))
    if hits >= 1:
        return 0.9
    return 0.30 if (org_tok & _OFF_DOMAIN) else 0.75


def to_mention(handle: str, fallback_name: str | None = None, *,
               context: str | None = None, entity_name: str | None = None,
               threshold: float = 0.7) -> str | None:
    """slug/handle → '@[Name](urn:li:organization:<id>)' (the ONLY form LinkedIn
    renders as a real mention), or None to abstain.

    When `context` (the card's headline + entities + relationships) is given and
    the entity name is collision-prone, the resolved org is verified against it;
    a confidence below `threshold` drops the tag rather than mis-tag a real org."""
    res = resolve(handle)
    if not res:
        return None
    name = (res.get("name") or fallback_name or _slug(handle)).strip()
    probe = entity_name or fallback_name or name
    if context and _collision_prone(probe):
        conf = _context_confidence(f"{res.get('name','')} {res.get('desc','')}",
                                   context, exclude=probe)
        if conf < threshold:
            print(f"    [handle] dropped '@{_slug(handle)}' → "
                  f"urn:li:organization:{res['id']} ({name}): context "
                  f"confidence {conf:.2f} < {threshold} — likely wrong same-named "
                  f"org", file=sys.stderr)
            return None
    return f"@[{name}](urn:li:organization:{res['id']})"
