"""Firestore-backed catalyst-card source — the LIVE cards the frontend serves.

Reads the KG's per-sector cards from a Firestore subcollection (e.g.
`CKG-Robotics/catalysts/items`), where each document is one card keyed by
`card_id` with the same schema as the old cards.json export (headline,
subtitle, entities, relationships, date, ...). Reading Firestore live is what
the robotics site itself does, so post text stays in lock-step with the live
per-card pages / og:images — no stale-snapshot drift like the static file had.

`collection` is a full subcollection PATH (odd number of segments), e.g.
"CKG-Robotics/catalysts/items". Used by `factory.build_source` for
`DATA_SOURCE_*_TYPE="firestore_cards"`.
"""
from __future__ import annotations

from datetime import datetime

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from .base import Source


class FirestoreCards(Source):
    def __init__(self, gcp_project: str, collection: str, **_):
        self.client = firestore.Client(project=gcp_project)
        self.collection = collection  # e.g. "CKG-Robotics/catalysts/items"

    def _coll(self):
        return self.client.collection(self.collection)

    def list_recent(self, since: datetime, limit: int = 50, **filters) -> list[dict]:
        # Cards carry a `date` string ("YYYY-MM-DD", lexicographically ordered).
        # A single-field range + order on the same field needs no composite index.
        since_str = since.strftime("%Y-%m-%d")
        q = (self._coll()
             .where(filter=FieldFilter("date", ">=", since_str))
             .order_by("date", direction=firestore.Query.DESCENDING)
             .limit(limit))
        return [{**d.to_dict(), "card_id": d.id} for d in q.stream()]

    def get(self, item_id: str) -> dict:
        snap = self._coll().document(item_id).get()
        if not snap.exists:
            raise KeyError(f"card '{item_id}' not in {self.collection}")
        return {**snap.to_dict(), "card_id": snap.id}

    def get_related(self, item_id: str) -> list[dict]:
        """Cards carry their `relationships` inline — return them verbatim."""
        try:
            return list(self.get(item_id).get("relationships") or [])
        except KeyError:
            return []
