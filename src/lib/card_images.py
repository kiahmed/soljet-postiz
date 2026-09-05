"""Which cards have a rendered PNG — the single source of truth for "ready".

Production rendering happens on GCP (Cloud Run `robotics-render`, triggered by
Pub/Sub after each ingest run) and lands in a GCS bucket
(gs://<RENDER_GCS_BUCKET>/<RENDER_GCS_PREFIX>/<card_id>.png) — that's what
robotics.arboryx.ai actually serves. A LOCAL checkout of the sibling KG repo can
also hold a dev-only render output at <KG_REPO>/data/exports/card_images/, but
that directory is not kept in sync with production and can go stale for weeks
without anyone noticing (it did: this repo's "ready" gate was reading that local
dir, found nothing rendered after 2026-07-27, and silently starved both posting
channels for over a week while GCS had 500+ fresh renders the whole time).

Preference order per tier: GCS (RENDER_GCS_BUCKET set) > local dir
(KG_REPO_PATH set) > no pipeline. A card without a render still posts, but on an
`attach` channel (LinkedIn) it goes out with no image — so batch runs skip
un-rendered cards by default (see requires_render).

Used by bin/post-status.py (reporting) and bin/daily.py (--ready-only), so both
answer "is this card ready?" identically.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config_loader import _TIER_DIR_BY_ID

REPO_ROOT = Path(__file__).resolve().parents[2]
_GCS_CACHE_PATH = REPO_ROOT / "data" / "render_gcs_cache.json"
_GCS_CACHE_TTL = 900  # 15 min — matches the scheduler watchdog cadence


def _gcs_config(tier) -> tuple[str, str] | None:
    """(bucket, prefix) for this tier's production render bucket, or None."""
    raw = getattr(tier, "raw", {}) or {}
    bucket = str(raw.get("RENDER_GCS_BUCKET", "") or "").strip()
    if not bucket:
        return None
    prefix = str(raw.get("RENDER_GCS_PREFIX", "") or "cards").strip().strip("/")
    return bucket, prefix


def _gcs_list_ids(bucket: str, prefix: str) -> set[str] | None:
    """All rendered card_ids under gs://bucket/prefix/, or None on any failure
    (missing dependency, auth, network) — callers must NOT treat None as 'empty',
    that reproduces the exact bug this module exists to fix."""
    try:
        from google.cloud import storage  # optional dep; see requirements.txt
    except ImportError:
        return None
    try:
        client = storage.Client()
        blobs = client.list_blobs(bucket, prefix=f"{prefix}/")
        ids = set()
        for b in blobs:
            name = b.name[len(prefix) + 1:]
            if name.endswith(".png"):
                ids.add(name[:-4])
        return ids
    except Exception:  # noqa: BLE001 — any GCS/auth error, fail to None
        return None


def _gcs_rendered_ids(tier) -> set[str] | None:
    """Cached (TTL 15 min) set of rendered card_ids from GCS. Positive results
    are effectively permanent (a render is never un-rendered); the cache exists
    to avoid re-listing 500+ objects on every invocation, not because the data
    goes stale quickly."""
    cfg = _gcs_config(tier)
    if not cfg:
        return None
    bucket, prefix = cfg
    key = f"{bucket}/{prefix}"
    cache: dict = {}
    try:
        cache = json.loads(_GCS_CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    row = cache.get(key) or {}
    if row.get("ids") is not None and time.time() - row.get("fetched_at", 0) < _GCS_CACHE_TTL:
        return set(row["ids"])

    ids = _gcs_list_ids(bucket, prefix)
    if ids is None:
        # Fetch failed: serve a stale cache rather than nothing — better to work
        # off data that's a few minutes old than to fail closed on a network blip.
        return set(row["ids"]) if row.get("ids") is not None else None

    cache[key] = {"ids": sorted(ids), "fetched_at": time.time()}
    try:
        _GCS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _GCS_CACHE_PATH.write_text(json.dumps(cache))
    except OSError:
        pass
    return ids


def png_dir(tier) -> Path | None:
    """This tier's KG render dir, or None if it has no card-render pipeline.

    A tier with no KG_REPO_PATH (e.g. the parent, which posts entry links) has no
    card PNGs → None, ALWAYS, so it can never borrow another tier's renders via
    the override. KG_CARD_IMAGES_DIR only backs up a tier that HAS a pipeline but
    whose relative path doesn't resolve here (e.g. from a git worktree — the
    config's `../` assumes the main checkout)."""
    kg = getattr(tier, "raw", {}).get("KG_REPO_PATH")
    if not kg:
        return None
    base = _TIER_DIR_BY_ID.get(getattr(tier, "id", ""), Path.cwd())
    resolved = (base / kg / "data" / "exports" / "card_images").resolve()
    if resolved.is_dir():
        return resolved
    env = os.getenv("KG_CARD_IMAGES_DIR")
    return Path(env) if env else resolved   # nonexistent → treated as "no dir"


def has_render(tier, card_id: str) -> bool:
    """True if this card's PNG exists. GCS (production) takes priority over the
    local dev directory when both are configured. False for tiers with no
    pipeline, and false (not true!) if GCS is configured but unreachable — never
    claim a render exists without having actually seen it."""
    if not card_id:
        return False
    if _gcs_config(tier):
        ids = _gcs_rendered_ids(tier)
        return card_id in ids if ids is not None else False
    d = png_dir(tier)
    if not d or not d.is_dir():
        return False
    return (d / f"{card_id}.png").is_file()


def renders_available(tier) -> bool:
    """True if this tier has a working render source at all — GCS reachable, or
    a local dir present — so callers can tell 'no pipeline' apart from
    'pipeline exists, this card isn't rendered'."""
    if _gcs_config(tier):
        return _gcs_rendered_ids(tier) is not None
    d = png_dir(tier)
    return bool(d and d.is_dir())


def requires_render(tier) -> bool:
    """True if this tier must NOT post a card that has no rendered PNG.

    Keyed on the tier DECLARING a card pipeline (KG_REPO_PATH), not on the
    render dir existing — so if the KG mount is missing or the path doesn't
    resolve, we fail CLOSED (post nothing) instead of silently publishing
    imageless posts to an `attach` channel. Override per tier with
    REQUIRE_CARD_IMAGE="false" if you ever genuinely want text-only cards."""
    raw = getattr(tier, "raw", {}) or {}
    override = str(raw.get("REQUIRE_CARD_IMAGE", "")).strip().lower()
    if override in ("true", "1", "yes"):
        return True
    if override in ("false", "0", "no"):
        return False
    # declares a card pipeline (production GCS or a local dev dir) → require it
    return bool(raw.get("RENDER_GCS_BUCKET") or raw.get("KG_REPO_PATH"))


def explain_missing(tier, card_id: str) -> str:
    """Human-readable reason a card is being skipped, so a missing mount/bucket
    is diagnosable instead of looking like an empty backlog."""
    cfg = _gcs_config(tier)
    if cfg:
        bucket, prefix = cfg
        if _gcs_rendered_ids(tier) is None:
            return (f"GCS bucket gs://{bucket}/{prefix}/ UNREACHABLE "
                    f"(missing google-cloud-storage? bad credentials?)")
        return f"no {prefix}/{card_id}.png in gs://{bucket}"
    d = png_dir(tier)
    if not d:
        return "tier declares no KG_REPO_PATH or RENDER_GCS_BUCKET"
    if not d.is_dir():
        return f"render dir MISSING at {d} (KG repo not mounted?)"
    return f"no {card_id}.png in {d}"
