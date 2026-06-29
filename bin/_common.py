"""Shared helpers for bin/* scripts."""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.lib.config_loader import Tier, load_tier  # noqa: E402
from src.lib.sources.base import Source  # noqa: E402
from src.lib.sources.factory import build_source as _factory_build_source  # noqa: E402


def parse_since(s: str) -> datetime:
    """Accept '1d', '24h', '7d', '90m', or ISO date."""
    m = re.fullmatch(r"(\d+)([dhm])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
        return datetime.now(timezone.utc) - delta
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Lightweight .env loader (no external dep)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def build_source(ds, tier: Tier) -> Source:
    """Construct a Source from a DataSource declaration."""
    return _factory_build_source(ds, tier)


def integration_ids_for(tier: Tier) -> list[str]:
    """Channels to publish to. Respects LINKEDIN_ENABLED gate AND, for branch
    tiers, the parent's `BRANCH_<NAME>_ENABLED` flag (mirrors arboryx-admin/
    frontend SECTOR_<NAME>_ENABLED pattern). A disabled branch returns []."""
    # Branch-tier gate — check parent's enable flag for this branch
    if tier.parent_id:
        from src.lib.config_loader import load_tier
        from src.lib.funnel import branch_enabled, branch_short_name
        parent = load_tier(tier.parent_id)
        if not branch_enabled(parent, branch_short_name(tier)):
            return []

    ids = []
    for c in tier.channels:
        if c == tier.raw.get("CHANNEL_LINKEDIN"):
            if os.getenv("LINKEDIN_ENABLED", "false").lower() == "true":
                ids.append(c)
            continue
        ids.append(c)
    return ids
