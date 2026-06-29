"""Build a Source from a DataSource declaration. Shared by CLI scripts and recipes."""
from __future__ import annotations

from .base import Source
from .cards_source import CardsJSON
from .duckdb_source import DuckDBKG
from .firestore_source import FirestoreFindings


def build_source(ds, tier) -> Source:
    if ds.type == "firestore":
        return FirestoreFindings(
            gcp_project=ds.params["gcp_project"],
            collection=ds.params["collection"],
        )
    if ds.type == "firestore_inherited":
        from ..config_loader import load_tier
        parent = load_tier(ds.params["inherit_from"])
        for ps in parent.sources:
            if ps.type == "firestore":
                return FirestoreFindings(
                    gcp_project=ps.params["gcp_project"],
                    collection=ps.params["collection"],
                    filter_category=ds.params.get("filter_category"),
                )
        raise RuntimeError(f"Parent tier '{parent.id}' has no firestore source to inherit")
    if ds.type == "duckdb":
        return DuckDBKG(path=ds.params["path"], base_dir=tier.dir)
    if ds.type == "cards_json":
        return CardsJSON(path=ds.params["path"], base_dir=tier.dir)
    raise ValueError(f"Unknown source type: {ds.type}")


def first_firestore(tier) -> FirestoreFindings | None:
    """Return the (possibly inherited) Firestore source for this tier."""
    for ds in tier.sources:
        if ds.type in ("firestore", "firestore_inherited"):
            return build_source(ds, tier)  # type: ignore
    return None
