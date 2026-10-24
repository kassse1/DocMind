"""Normalize extracted text into clean plain text while keeping paragraph boundaries.

Output contract: "\n" line endings, single spaces within lines, at most one blank line
("\n\n") between paragraphs, no control characters, Unicode NFC.
"""

import re
import unicodedata

# Invisible characters that only break search and tokenization:
# soft hyphen, zero-width space, word joiner, byte-order mark.
_INVISIBLE = dict.fromkeys(map(ord, "­​⁠﻿"))
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")
# Any whitespace except "\n": tabs, NBSP, thin spaces, vertical tab, form feed...
_INLINE_WHITESPACE = re.compile(r"[^\S\n]+")
_EXTRA_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    # NFC, not NFKC: NFKC would rewrite characters such as superscripts and fractions.
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", "\n\n").replace(" ", "\n")
    text = text.translate(_INVISIBLE)
    text = _CONTROL_CHARS.sub(" ", text)
    text = _INLINE_WHITESPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _EXTRA_BLANK_LINES.sub("\n\n", text)
    return text.strip()
