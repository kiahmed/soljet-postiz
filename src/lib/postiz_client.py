"""Postiz public-API client — drafts, scheduled posts, immediate posts.

Endpoints:
  POST /api/public/v1/posts   — create posts (type=draft|schedule|now)
  POST /api/public/v1/upload  — upload media (multipart)

Auth: per-org API key, header `Authorization: <api_key>`.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import requests

DEFAULT_BASE = os.getenv("POSTIZ_API_URL", "https://dev.arboryx.ai")


class PostizClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or os.environ["POSTIZ_API_KEY"]
        self.base = (base_url or DEFAULT_BASE).rstrip("/")
        self.h = {"Authorization": self.api_key}

    def upload(self, path: Path) -> dict:
        # Postiz validators check extension by file name — strip spaces so e.g.
        # 'just icon.png' is sent as 'just_icon.png'.
        send_name = path.name.replace(" ", "_")
        with path.open("rb") as f:
            r = requests.post(
                f"{self.base}/api/public/v1/upload",
                headers=self.h,
                files={"file": (send_name, f)},
                timeout=180,
            )
        if r.status_code >= 400:
            raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url}\nbody: {r.text[:1500]}", response=r)
        return r.json()

    def create_post(
        self,
        *,
        parts: list[str] | None = None,
        text: str | None = None,
        integration_ids: list[str],
        mode: str = "draft",
        publish_date: str | None = None,
        media: list[dict] | None = None,
        media_ids: list[str] | None = None,  # deprecated; use media=[{id,path}]
        tags: list[str] | None = None,
    ) -> dict:
        """mode: 'draft' | 'schedule' | 'now'.

        Pass `parts=[...]` for a thread (each entry becomes one sub-tweet via
        Postiz's `value: [...]` shape). Pass `text=` for a single post.
        Media attaches only to the first sub-tweet of a thread.
        """
        if mode not in ("draft", "schedule", "now"):
            raise ValueError(f"mode must be draft|schedule|now, got {mode}")
        if mode == "schedule" and not publish_date:
            raise ValueError("publish_date required when mode='schedule'")

        if parts is None:
            if not text:
                raise ValueError("either parts=[...] or text= required")
            parts = [text]
        if not parts:
            raise ValueError("parts cannot be empty")

        # Postiz validators require `date` for ALL modes (including draft).
        if mode == "schedule":
            date_iso = publish_date
        elif mode == "now":
            date_iso = datetime.now(timezone.utc).isoformat()
        else:  # draft — Postiz still wants a valid ISO date; future placeholder.
            date_iso = publish_date or datetime.now(timezone.utc).isoformat()

        image_entries: list[dict] = []
        if media:
            image_entries = [{"id": m["id"], "path": m["path"]} for m in media]
        elif media_ids:
            image_entries = [{"id": m} for m in media_ids]  # legacy; will fail validation

        # Thread shape: media on first sub-tweet only, rest are text-only.
        value_array = [
            {
                "content": part,
                "image": image_entries if i == 0 else [],
            }
            for i, part in enumerate(parts)
        ]

        payload = {
            "type": mode,
            "date": date_iso,
            "shortLink": False,
            "tags": tags or [],
            "posts": [
                {
                    "integration": {"id": iid},
                    "value": value_array,
                    "settings": {"who_can_reply_post": "everyone"},
                }
                for iid in integration_ids
            ],
        }

        r = requests.post(
            f"{self.base}/api/public/v1/posts",
            headers={**self.h, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if r.status_code >= 400:
            raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url}\nbody: {r.text[:1500]}", response=r)
        return r.json()

    # backwards-compatible thin wrapper used by older code paths
    def create_draft(self, *, text: str, integration_ids: list[str], media_ids: list[str] | None = None) -> dict:
        return self.create_post(
            text=text, integration_ids=integration_ids, mode="draft", media_ids=media_ids
        )
