"""Source adapter for the catalyst-knowledge-graph's `cards.json` artifact.

Cards.json is the curated, post-ready output of the KG pipeline — each card
already has `headline`, `entities`, `relationships` (with from/to/rel/
mechanism/confidence/impact_magnitude). Reading it directly skips the LLM
extraction step we'd otherwise run for each branch single-recipe post.

Resolves `path` relative to the tier's directory (same convention as
DuckDB source). Used by `factory.build_source` for `DATA_SOURCE_*_TYPE="cards_json"`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .base import Source


class CardsJSON(Source):
    def __init__(self, path: str, base_dir: Path):
        p = Path(path)
        self.path = (base_dir / p).resolve() if not p.is_absolute() else p
        self._cache: dict | None = None

    def _load(self) -> dict:
        if self._cache is None:
            self._cache = json.loads(self.path.read_text())
        return self._cache

    def _cards(self) -> list[dict]:
        data = self._load()
        # Top-level shape: {schema_version, sector, stats, cards: [...], graph: ...}
        return data.get("cards", []) if isinstance(data, dict) else []

    def list_recent(self, since: datetime, limit: int = 50, **filters) -> list[dict]:
        out = []
        for c in self._cards():
            d = c.get("date")
            if d:
                try:
                    dt = datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
                except ValueError:
                    dt = None
                if dt and dt < since:
                    continue
            out.append(c)
        out.sort(key=lambda c: c.get("date") or "", reverse=True)
        return out[:limit]

    def get(self, item_id: str) -> dict:
        for c in self._cards():
            if c.get("card_id") == item_id:
                return c
        raise KeyError(f"card_id '{item_id}' not in {self.path}")

    def get_related(self, item_id: str) -> list[dict]:
        """Cards already carry their own `relationships` inline — no
        cross-card join needed. Returning the relationships verbatim so
        callers can treat them like edges from a graph adapter."""
        try:
            return list(self.get(item_id).get("relationships") or [])
        except KeyError:
            return []
