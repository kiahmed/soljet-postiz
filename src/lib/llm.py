"""Unified LLM client — OpenAI primary, Gemini fallback for text; Gemini for images.

Routes:
- `chat(system, user)` — try OpenAI; on any failure (quota, network, parse, no
  key), retry with Gemini (`GEMINI_TEXT_MODEL`, default `gemini-2.5-flash-lite`).
  Returns the text or None if both providers fail.
- `generate_image(prompt, out_path)` — Gemini-only via `GEMINI_IMAGE_MODEL`
  (default `gemini-3-pro-image-preview`). Writes the image bytes to `out_path`,
  validates the output is a real image of usable size, deletes it if not.
  Returns True on success.

Both providers' keys come from env (`OPENAI_API_KEY`, `GEMINI_API_KEY`).
Set `LLM_DEBUG=true` to surface provider errors on stderr.
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import requests

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_GEMINI_TEXT = "gemini-2.5-flash-lite"
DEFAULT_GEMINI_IMAGE = "gemini-3-pro-image-preview"

MIN_IMAGE_BYTES = 5_000          # below = corrupt / placeholder
MIN_IMAGE_DIMENSIONS = (600, 400)


def _debug() -> bool:
    return os.getenv("LLM_DEBUG", "false").lower() == "true"


# ---------- Text chat (OpenAI primary, Gemini fallback) ----------

def chat(system: str, user: str, *, max_tokens: int = 256) -> str | None:
    """Single-shot chat. Returns the response text or None if both providers fail."""
    text = _openai_chat(system, user, max_tokens)
    if text is not None:
        return text
    return _gemini_chat(system, user, max_tokens)


def _openai_chat(system: str, user: str, max_tokens: int) -> str | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        if _debug():
            print("[llm] openai package missing", file=sys.stderr)
        return None
    try:
        client = OpenAI(api_key=api_key)
        r = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        out = r.choices[0].message.content
        return out.strip() if out else None
    except Exception as e:
        if _debug():
            print(f"[llm] OpenAI chat failed: {e}", file=sys.stderr)
        return None


def _gemini_chat(system: str, user: str, max_tokens: int) -> str | None:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    model = os.getenv("GEMINI_TEXT_MODEL", DEFAULT_GEMINI_TEXT)
    url = f"{GEMINI_BASE}/{model}:generateContent?key={api_key}"
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            if _debug():
                print(f"[llm] Gemini chat {r.status_code}: {r.text[:400]}", file=sys.stderr)
            return None
        data = r.json()
        for cand in data.get("candidates") or []:
            for part in cand.get("content", {}).get("parts") or []:
                if part.get("text"):
                    return part["text"].strip()
        return None
    except Exception as e:
        if _debug():
            print(f"[llm] Gemini chat exception: {e}", file=sys.stderr)
        return None


# ---------- Image generation (Gemini only) ----------

def generate_image(prompt: str, out_path: Path | str) -> bool:
    """Write a generated image to `out_path` and return True on success.

    Validates the output: must decode as an image, size >= MIN_IMAGE_BYTES, and
    dimensions >= MIN_IMAGE_DIMENSIONS. If validation fails, the file is
    deleted and we return False — never a half-baked image left on disk.
    """
    out = Path(out_path)
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return False
    model = os.getenv("GEMINI_IMAGE_MODEL", DEFAULT_GEMINI_IMAGE)
    url = f"{GEMINI_BASE}/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
    }
    try:
        r = requests.post(url, json=payload, timeout=120)
        if r.status_code != 200:
            if _debug():
                print(f"[llm] Gemini image {r.status_code}: {r.text[:500]}", file=sys.stderr)
            return False
        data = r.json()
        img_bytes = _extract_inline_image(data)
        if not img_bytes:
            if _debug():
                print(f"[llm] Gemini image returned no inline data: {str(data)[:300]}", file=sys.stderr)
            return False
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(img_bytes)
        if not _valid_image(out):
            if _debug():
                print(f"[llm] Gemini image failed validation, deleted: {out}", file=sys.stderr)
            try:
                out.unlink()
            except OSError:
                pass
            return False
        return True
    except Exception as e:
        if _debug():
            print(f"[llm] Gemini image exception: {e}", file=sys.stderr)
        return False


def _extract_inline_image(data: dict) -> bytes | None:
    """Walk Gemini's response shape and return the first inline image bytes."""
    for cand in data.get("candidates") or []:
        for part in cand.get("content", {}).get("parts") or []:
            inline = part.get("inline_data") or part.get("inlineData")
            if inline and inline.get("data"):
                try:
                    return base64.b64decode(inline["data"])
                except Exception:
                    continue
    return None


def _valid_image(path: Path) -> bool:
    if path.stat().st_size < MIN_IMAGE_BYTES:
        return False
    try:
        from PIL import Image
        with Image.open(path) as img:
            img.verify()
        # Re-open after verify (verify() leaves the file in a useless state)
        with Image.open(path) as img:
            w, h = img.size
        return w >= MIN_IMAGE_DIMENSIONS[0] and h >= MIN_IMAGE_DIMENSIONS[1]
    except Exception:
        return False
