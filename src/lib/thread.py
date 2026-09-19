"""Thread-splitter — break a long post into ≤ N-char sub-tweets with ' i/n' markers.

X (free tier) caps tweets at 280 chars. Posts longer than that are split into
a numbered thread. Postiz's API takes a thread as `posts[].value: [{...}, ...]`
where each entry is one sub-tweet; media attaches only to the first.

Splitter prefers strong boundaries (paragraph > sentence > line > whitespace)
and falls back to a hard cut only when no whitespace exists in the budget.

Length is measured raw (not t.co-normalised). The default `marker_reserve=8`
covers ' 99/99' (6 chars) plus a small buffer for short URLs that X inflates
to 23 chars in its own counter — adequate for typical posts. If your post has
multiple short URLs near the boundary, lower max_chars or call with a larger
marker_reserve.
"""
from __future__ import annotations

import re

_BOUNDARIES = [
    re.compile(r"\n\n+"),                # paragraph
    re.compile(r"(?<=[.!?])\s+"),        # end of sentence
    re.compile(r"\n"),                    # any line break
    re.compile(r"\s+"),                   # any whitespace
]


def max_chars_for_channel(label: str) -> int:
    """Char budget to pass to split_for_thread(), per channel. X's 280 cap is
    the reason the splitter exists; LinkedIn's real organic-post cap is
    ~3000, so anything our composers produce fits in one native post there.
    Applying X's limit unconditionally to every channel — the pre-existing
    behavior — never mattered while everything topped out at 280 chars; a
    graph post's ~500-char budget is the first thing to actually exceed it,
    and it silently turned every LinkedIn graph post into two separate posts
    instead of one."""
    return 280 if label.upper() == "X" else 3000


def split_for_thread(text: str, max_chars: int = 280, marker_reserve: int = 8,
                     continuation_prefix: str = "") -> list[str]:
    """Return [text] if it fits; otherwise a list of ' i/n'-suffixed chunks ≤ max_chars.

    continuation_prefix: prepended to every part AFTER the first (never the
    first — it already opens with the real content). A part seen out of
    thread order (a quote-tweet, a direct permalink) otherwise reads as an
    orphaned fragment with no idea what it's about. Skipped for any
    individual part where adding it would push that part over max_chars —
    this never trades self-containment for an over-length tweet."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    budget = max_chars - marker_reserve
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= budget:
            chunks.append(rest.strip())
            break
        cut = budget
        advance = budget
        for pat in _BOUNDARIES:
            matches = list(pat.finditer(rest, 0, budget + 1))
            if matches:
                m = matches[-1]
                cut = m.start()
                advance = m.end()
                break
        chunks.append(rest[:cut].strip())
        rest = rest[advance:].lstrip()

    n = len(chunks)
    parts = [f"{c} {i + 1}/{n}" for i, c in enumerate(chunks)]
    if continuation_prefix:
        for i in range(1, len(parts)):
            candidate = f"{continuation_prefix} — {parts[i]}"
            if len(candidate) <= max_chars:
                parts[i] = candidate
    return parts
