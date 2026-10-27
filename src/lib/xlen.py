"""Length of an X post the way Postiz validates it.

Postiz rejects an X part ("post is too long, please fix it") when EITHER its
raw JS string length (UTF-16 units — an emoji is 2, a link counts in full, UTM
query included) OR its twitter-text weighted length exceeds 280. Plain
`len()` misses both: it counts an emoji as 1 and, worse, the weighted rule
charges 2 for every char outside X's weight-1 ranges (↳ → are 2; — is 1) while
a URL is a flat 23. Mirrors libraries/helpers/src/utils/count.length.ts +
posts.service.ts `tooLong` (max of the two).
"""
from __future__ import annotations

import re

_URL = re.compile(r"https?://[^\s]+")
_LIGHT = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))  # weight-1 ranges


def _weighted(text: str) -> int:
    n = 23 * len(_URL.findall(text))
    for ch in _URL.sub("", text):
        cp = ord(ch)
        n += 1 if any(a <= cp <= b for a, b in _LIGHT) else 2
    return n


def x_len(text: str) -> int:
    """max(UTF-16 length, twitter-text weighted length) — what Postiz compares to 280."""
    return max(len(text.encode("utf-16-le")) // 2, _weighted(text))
