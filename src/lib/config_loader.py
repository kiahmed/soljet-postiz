"""Tier config loader.

Parses shell-style key="value" .config files (same shape as
arboryx-admin/arboryx_admin_backend.config) and resolves parent-tier
inheritance. Values flow parent → child; child overrides on conflict.
Data sources from parent are kept and extended (not replaced) unless
the child explicitly redeclares the same DATA_SOURCE_N_TYPE slot.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTS_ROOT = REPO_ROOT / "products"

_ENV_LOADED = False
_ENV_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}|\$([A-Z0-9_]+)")


def _ensure_env_loaded() -> None:
    """Populate os.environ from repo-root .env (setdefault — never clobbers a
    value already exported or loaded by a bin/* script's load_dotenv())."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _expand_env(val: str) -> str:
    """Expand ${VAR}/$VAR references against the environment (.env-backed).
    Unknown vars resolve to '' so a missing id drops the channel / customer
    rather than leaking a literal ${...} token downstream."""
    if "$" not in val:
        return val
    _ensure_env_loaded()
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1) or m.group(2), ""), val)

_TIER_DIR_BY_ID = {
    "arboryx": PRODUCTS_ROOT / "arboryx.ai",
    "arboryx.robotics": PRODUCTS_ROOT / "arboryx.ai" / "branches" / "robotics",
}


@dataclass
class DataSource:
    type: str
    params: dict = field(default_factory=dict)


@dataclass
class Tier:
    id: str
    name: str
    parent_id: str | None
    dir: Path
    raw: dict
    sources: list[DataSource]
    channels: list[str]
    customer_id: str | None
    purpose: str
    sectors: list[str]
    # Per-channel imagery policy, keyed by lowercased channel name
    # ("x", "linkedin"): "link_card" | "attach". Absent channel → legacy behavior.
    imagery_policy: dict = field(default_factory=dict)


def _parse_config(path: Path) -> dict:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'([A-Z0-9_]+)=(.*)', line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        # shlex handles "quoted values with spaces"
        val = shlex.split(val)[0] if val else ""
        # expand ${VAR} refs so secret/operational ids stay in .env, not git
        out[key] = _expand_env(val)
    return out


def _collect_sources(raw: dict) -> list[DataSource]:
    sources: list[DataSource] = []
    i = 1
    while True:
        type_key = f"DATA_SOURCE_{i}_TYPE"
        if type_key not in raw:
            break
        params = {
            k.replace(f"DATA_SOURCE_{i}_", "", 1).lower(): v
            for k, v in raw.items()
            if k.startswith(f"DATA_SOURCE_{i}_") and k != type_key
        }
        sources.append(DataSource(type=raw[type_key], params=params))
        i += 1
    return sources


def _collect_imagery_policy(raw: dict) -> dict[str, str]:
    """IMAGERY_POLICY_<CHANNEL>=<policy> → {channel_lower: policy_lower}."""
    out: dict[str, str] = {}
    for k, v in raw.items():
        if k.startswith("IMAGERY_POLICY_") and v:
            out[k.replace("IMAGERY_POLICY_", "", 1).lower()] = v.strip().lower()
    return out


def _collect_channels(raw: dict) -> list[str]:
    out = []
    for k, v in raw.items():
        if k.startswith("CHANNEL_") and v:
            out.append(v)
    return out


def load_tier(tier_id: str) -> Tier:
    if tier_id not in _TIER_DIR_BY_ID:
        raise KeyError(f"Unknown tier '{tier_id}'. Known: {list(_TIER_DIR_BY_ID)}")
    tier_dir = _TIER_DIR_BY_ID[tier_id]
    raw = _parse_config(tier_dir / "tier.config")

    parent_id = raw.get("TIER_PARENT") or None
    parent: Tier | None = load_tier(parent_id) if parent_id else None

    sources = _collect_sources(raw)
    if parent:
        # branch inherits parent sources unless it declared a same-type override
        own_types = {s.type for s in sources}
        for ps in parent.sources:
            if ps.type in own_types:
                continue
            sources.append(ps)

    channels = _collect_channels(raw)
    if raw.get("CHANNELS_INHERIT_FROM") and parent:
        # additive — branch's dedicated channels (if any) layered on top
        inherited = list(parent.channels)
        for c in channels:
            if c not in inherited:
                inherited.append(c)
        channels = inherited

    sectors = []
    if raw.get("SECTORS"):
        sectors = [s.strip() for s in raw["SECTORS"].split(",")]
    elif parent:
        sectors = parent.sectors

    # Per-channel imagery policy: inherit parent, then own keys override.
    imagery_policy = dict(parent.imagery_policy) if parent else {}
    imagery_policy.update(_collect_imagery_policy(raw))

    return Tier(
        id=raw["TIER_ID"],
        name=raw.get("TIER_NAME", raw["TIER_ID"]),
        parent_id=parent_id,
        dir=tier_dir,
        raw=raw,
        sources=sources,
        channels=channels,
        customer_id=raw.get("POSTIZ_CUSTOMER_ID") or (parent.customer_id if parent else None),
        purpose=raw.get("POSTING_PURPOSE", ""),
        sectors=sectors,
        imagery_policy=imagery_policy,
    )


def context_chain(tier: Tier) -> list[Path]:
    """Return parent context.md → branch context.md, in compose order."""
    chain: list[Path] = []
    if tier.parent_id:
        chain.extend(context_chain(load_tier(tier.parent_id)))
    ctx = tier.dir / tier.raw.get("CONTEXT_FILE", "context.md")
    if ctx.exists():
        chain.append(ctx)
    return chain
