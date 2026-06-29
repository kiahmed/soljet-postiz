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


def split_for_thread(text: str, max_chars: int = 280, marker_reserve: int = 8) -> list[str]:
    """Return [text] if it fits; otherwise a list of ' i/n'-suffixed chunks ≤ max_chars."""
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
    return [f"{c} {i + 1}/{n}" for i, c in enumerate(chunks)]
