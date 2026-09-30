"""Reflex post store — public.best_signal_posts in news-reactor's Supabase.

Reflex (facades-news-reactor) writes a FINISHED post here (image, caption,
hashtags) and announces it on Pub/Sub; bin/reflex_poster.py reads it back at
publish time and flips `posted`. That flag is the ONLY column we may write.

Connection: the owner/pooler URL (RLS is on — the anon key sees nothing), from
$REFLEX_SUPABASE_DB_URL (Secret Manager `reflex-db-url` on Cloud Run). This is a
different Supabase project from EdgeLane's SUPABASE_URL — never mix them.

Always read the row at publish time: a Reflex re-render updates the row in
place and sends no new message.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

_COLS = ("id", "event_id", "posted", "image_png", "caption", "caption_text",
         "hashtags", "headline", "direction", "move_bps", "move_usd",
         "minutes_to_peak", "armed_at", "created_at")


def _row(cur, rec) -> dict | None:
    if rec is None:
        return None
    d = dict(zip([c.name for c in cur.description], rec))
    if d.get("image_png") is not None:
        d["image_png"] = bytes(d["image_png"])
    return d


class ReflexPostsDB:
    def __init__(self, url: str | None = None, table: str = "public.best_signal_posts"):
        self.url = (url or os.getenv("REFLEX_SUPABASE_DB_URL") or "").strip()
        self.table = table

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _connect(self):
        import psycopg2
        return psycopg2.connect(self.url, connect_timeout=10,
                                application_name="reflex-poster")

    def peek(self, post_id: int, *, include_posted: bool = False) -> dict | None:
        """Read-only fetch (dry runs, inspection). Takes no lock, writes nothing."""
        where = "" if include_posted else " and not posted"
        conn = self._connect()
        try:
            conn.set_session(readonly=True, autocommit=True)
            with conn.cursor() as cur:
                cur.execute(f"select {', '.join(_COLS)} from {self.table} "
                            f"where id = %s{where}", (post_id,))
                return _row(cur, cur.fetchone())
        finally:
            conn.close()

    @contextmanager
    def claim(self, post_id: int):
        """Yield (row, mark_posted) with the row locked for this transaction.

        `select ... for update skip locked` so a concurrent redelivery of the
        same message sees None instead of double-posting. The lock is held while
        the caller publishes (a few seconds); call mark_posted() once published
        — it's committed on a clean exit, rolled back if the block raises.
        row is None when the post is already posted, missing, or held elsewhere.
        """
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"select {', '.join(_COLS)} from {self.table} "
                            "where id = %s and not posted for update skip locked",
                            (post_id,))
                row = _row(cur, cur.fetchone())

                def mark_posted() -> None:
                    cur.execute(f"update {self.table} set posted = true where id = %s",
                                (post_id,))

                yield row, mark_posted
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
