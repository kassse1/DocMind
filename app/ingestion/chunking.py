"""Split extracted page text into overlapping, token-bounded chunks for retrieval.

Input is the text produced by parsers.extract_text(), which is already normalized
(normalize.py). Offsets are positions in that text, so text[char_start:char_end] is
exactly the chunk's content; Phase 7 relies on this to verify quoted citations.

Algorithm (deterministic):
1. Split recursively, coarse to fine: paragraphs ("\\n\\n"), lines, sentences, words,
   then a hard cut on token boundaries. A span is split further only while it has more
   tokens than the budget, so paragraphs that fit stay whole.
2. Greedily pack the pieces into chunks of at most `chunk_size` tokens.
3. Each new chunk starts with the previous chunk's tail (at most `overlap` tokens, cut at
   the coarsest boundary that fits), followed by at least one piece the previous chunk
   did not contain. That rule guarantees progress: no infinite loop, no duplicate chunk.

Chunks never cross a page boundary, so each chunk has exactly one page number.
Separators are whitespace only: every non-whitespace character ends up in some chunk.
"""

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import NamedTuple, Protocol


class Tokenizer(Protocol):
    def token_offsets(self, text: str) -> list[tuple[int, int]]:
        """Character span of every token in `text`, excluding special tokens."""
        ...


@dataclass(frozen=True)
class Chunk:
    chunk_index: int
    text: str
    # 1-based PDF page; None for formats without pages (DOCX, TXT).
    page_number: int | None
    # Offsets into the normalized text of that page (or of the whole document if no pages).
    char_start: int
    char_end: int
    token_count: int


class _Piece(NamedTuple):
    start: int
    end: int
    tokens: int


_SEPARATORS = (
    re.compile(r"\n\n"),
    re.compile(r"\n"),
    # Whitespace after sentence-ending punctuation, optionally followed by a closing quote
    # or bracket. Works for English, Russian and Kazakh, which share this punctuation.
    re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…][\"'»”)\]])\s+"),
    re.compile(r"\s+"),
)
# Spans longer than budget * this are split without being tokenized first: natural-language
# tokens average a few characters, so such a span cannot fit and tokenizing it is wasted work.
_MAX_CHARS_PER_TOKEN = 16


class Chunker:
    def __init__(self, tokenizer: Tokenizer, chunk_size: int, overlap: int) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be at least 1")
        if not 0 <= overlap < chunk_size:
            raise ValueError("overlap must be >= 0 and smaller than chunk_size")
        self.tokenizer = tokenizer
        self.chunk_size = chunk_size
        self.overlap = overlap

    def count_tokens(self, text: str) -> int:
        return len(self.tokenizer.token_offsets(text))

    def chunk_pages(self, pages: Iterable[tuple[int | None, str]]) -> Iterator[Chunk]:
        """Chunk pages in order; chunk_index is sequential across the whole document."""
        index = 0
        for page_number, text in pages:
            for start, end in self.split(text):
                chunk_text = text[start:end]
                yield Chunk(
                    chunk_index=index,
                    text=chunk_text,
                    page_number=page_number,
                    char_start=start,
                    char_end=end,
                    token_count=self.count_tokens(chunk_text),
                )
                index += 1

    def split(self, text: str) -> list[tuple[int, int]]:
        """Return (start, end) spans of the chunks of one page."""
        spans: list[tuple[int, int]] = []
        current: list[_Piece] = []
        current_tokens = 0
        for piece in self._pieces(text, 0, len(text), 0, self.chunk_size):
            if current and current_tokens + piece.tokens > self.chunk_size:
                spans.append((current[0].start, current[-1].end))
                current = self._overlap_tail(text, current)
                current_tokens = sum(p.tokens for p in current)
                # The carried tail must leave room for the new piece; drop from the front.
                while current and current_tokens + piece.tokens > self.chunk_size:
                    current_tokens -= current.pop(0).tokens
            current.append(piece)
            current_tokens += piece.tokens
        if current:
            spans.append((current[0].start, current[-1].end))
        return spans

    def _overlap_tail(self, text: str, chunk: list[_Piece]) -> list[_Piece]:
        tail: list[_Piece] = []
        total = 0
        for piece in reversed(chunk):
            if total + piece.tokens <= self.overlap:
                tail.insert(0, piece)
                total += piece.tokens
                continue
            # This piece is too big to carry whole: carry its trailing sub-pieces instead.
            remaining = self.overlap - total
            if remaining > 0:
                subs = list(self._pieces(text, piece.start, piece.end, 0, remaining))
                taken: list[_Piece] = []
                for sub in reversed(subs):
                    if total + sub.tokens > self.overlap:
                        break
                    taken.insert(0, sub)
                    total += sub.tokens
                tail = taken + tail
            break
        return tail

    def _pieces(self, text: str, start: int, end: int, level: int, budget: int) -> Iterator[_Piece]:
        """Yield trimmed, non-empty spans of at most `budget` tokens covering [start, end)."""
        start, end = _trim(text, start, end)
        if start >= end:
            return
        if end - start <= budget * _MAX_CHARS_PER_TOKEN:
            tokens = self.count_tokens(text[start:end])
            if tokens <= budget:
                yield _Piece(start, end, tokens)
                return
        while level < len(_SEPARATORS):
            parts = _split_on(text, start, end, _SEPARATORS[level])
            level += 1
            if len(parts) > 1:
                for part_start, part_end in parts:
                    yield from self._pieces(text, part_start, part_end, level, budget)
                return
        yield from self._hard_cut(text, start, end, budget)

    def _hard_cut(self, text: str, start: int, end: int, budget: int) -> Iterator[_Piece]:
        """Cut a span without separators (e.g. a long URL, or CJK text) on token boundaries."""
        window = budget * _MAX_CHARS_PER_TOKEN
        for window_start in range(start, end, window):
            window_end = min(window_start + window, end)
            offsets = self.tokenizer.token_offsets(text[window_start:window_end])
            if not offsets:
                continue
            for i in range(0, len(offsets), budget):
                group = offsets[i : i + budget]
                piece_start = window_start + group[0][0] if i else window_start
                is_last = i + budget >= len(offsets)
                piece_end = window_end if is_last else window_start + offsets[i + budget][0]
                piece_start, piece_end = _trim(text, piece_start, piece_end)
                if piece_start < piece_end:
                    yield _Piece(piece_start, piece_end, len(group))


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _split_on(text: str, start: int, end: int, separator: re.Pattern[str]) -> list[tuple[int, int]]:
    parts = []
    position = start
    for match in separator.finditer(text, start, end):
        if match.start() > position:
            parts.append((position, match.start()))
        position = match.end()
    if position < end:
        parts.append((position, end))
    return parts
