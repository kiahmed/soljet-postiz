"""Firestore findings source — reads marketresearch-agents `findings` collection."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from .base import Source


class FirestoreFindings(Source):
    def __init__(self, gcp_project: str, collection: str, filter_category: str | None = None, **_):
        self.client = firestore.Client(project=gcp_project)
        self.collection = collection
        self.filter_category = filter_category

    def _coll(self):
        return self.client.collection(self.collection)

    def list_recent(self, since: datetime, limit: int = 50, **filters) -> list[dict]:
        category = filters.get("category", self.filter_category)
        # Findings are persisted by the strategist with field name `timestamp`
        # (YYYY-MM-DD string, lexicographically comparable).
        since_str = since.strftime("%Y-%m-%d")
        q = self._coll().where(filter=FieldFilter("timestamp", ">=", since_str))
        if category:
            q = q.where(filter=FieldFilter("category", "==", category))
        q = q.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit)
        return [{"id": d.id, **d.to_dict()} for d in q.stream()]

    def get(self, item_id: str) -> dict[str, Any]:
        snap = self._coll().document(item_id).get()
        if not snap.exists:
            raise KeyError(f"Finding {item_id} not found")
        return {"id": snap.id, **snap.to_dict()}
