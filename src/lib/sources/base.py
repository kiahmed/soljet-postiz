"""Common interface for tier data sources."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime


class Source(ABC):
    @abstractmethod
    def list_recent(self, since: datetime, limit: int = 50, **filters) -> list[dict]:
        ...

    @abstractmethod
    def get(self, item_id: str) -> dict:
        ...

    def get_related(self, item_id: str) -> list[dict]:
        return []
