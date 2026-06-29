"""DuckDB KG source — reads catalysts/entities/relationships from a tier's
.duckdb file. Schema-flexible: introspects on first use so this works
against the current robotics.duckdb without us pinning a schema."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb

from .base import Source


class DuckDBKG(Source):
    """Adapts to whatever tables are present.

    Catalyst-table heuristic: a table whose name contains 'catalyst' or 'card'
    or — failing that — the table with the most rows that has a date-ish column.
    """

    def __init__(self, path: str, base_dir: Path | None = None, **_):
        p = Path(path)
        if base_dir and not p.is_absolute():
            p = (base_dir / p).resolve()
        self.path = p
        self._con: duckdb.DuckDBPyConnection | None = None
        self._tables: list[str] | None = None

    def _conn(self):
        if self._con is None:
            self._con = duckdb.connect(str(self.path), read_only=True)
        return self._con

    def tables(self) -> list[str]:
        if self._tables is None:
            rows = self._conn().execute("SHOW TABLES").fetchall()
            self._tables = [r[0] for r in rows]
        return self._tables

    def schema(self, table: str) -> list[tuple[str, str]]:
        rows = self._conn().execute(f"PRAGMA table_info('{table}')").fetchall()
        return [(r[1], r[2]) for r in rows]  # (column_name, type)

    def _catalyst_table(self) -> str:
        for t in self.tables():
            lt = t.lower()
            if "catalyst" in lt or "card" in lt:
                return t
        # fallback: largest table
        sized = []
        for t in self.tables():
            n = self._conn().execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            sized.append((n, t))
        sized.sort(reverse=True)
        if not sized:
            raise RuntimeError(f"No tables in {self.path}")
        return sized[0][1]

    def list_recent(self, since: datetime, limit: int = 50, **_) -> list[dict]:
        table = self._catalyst_table()
        cols = [c for c, _ in self.schema(table)]
        date_col = next((c for c in cols if c.lower() in ("created_at", "date", "timestamp", "ingested_at")), None)
        sql = f'SELECT * FROM "{table}"'
        if date_col:
            sql += f' WHERE "{date_col}" >= ?'
            sql += f' ORDER BY "{date_col}" DESC LIMIT {int(limit)}'
            rows = self._conn().execute(sql, [since]).fetchdf()
        else:
            sql += f" LIMIT {int(limit)}"
            rows = self._conn().execute(sql).fetchdf()
        return rows.to_dict(orient="records")

    def get(self, item_id: str) -> dict:
        table = self._catalyst_table()
        cols = [c for c, _ in self.schema(table)]
        id_col = next((c for c in cols if c.lower() in ("id", "catalyst_id", "card_id")), cols[0])
        rows = self._conn().execute(
            f'SELECT * FROM "{table}" WHERE "{id_col}" = ? LIMIT 1', [item_id]
        ).fetchdf()
        if rows.empty:
            raise KeyError(f"Catalyst {item_id} not found in {table}")
        return rows.iloc[0].to_dict()

    def get_related(self, item_id: str) -> list[dict]:
        # Best-effort: look for a relationships/edges table referencing this id.
        for t in self.tables():
            lt = t.lower()
            if "relationship" not in lt and "edge" not in lt and "link" not in lt:
                continue
            cols = [c for c, _ in self.schema(t)]
            ref_cols = [c for c in cols if c.lower() in ("source_id", "src_id", "from_id", "catalyst_id", "subject_id")]
            for rc in ref_cols:
                rows = self._conn().execute(
                    f'SELECT * FROM "{t}" WHERE "{rc}" = ? LIMIT 50', [item_id]
                ).fetchdf()
                if not rows.empty:
                    return rows.to_dict(orient="records")
        return []
