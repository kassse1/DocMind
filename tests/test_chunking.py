import random
import re

import pytest

from app.ingestion.chunking import Chunk, Chunker


class WordTokenizer:
    """One token per whitespace-separated word: token counts are easy to reason about."""

    def token_offsets(self, text: str) -> list[tuple[int, int]]:
        return [m.span() for m in re.finditer(r"\S+", text)]


class CharTokenizer:
    """One token per non-space character, like CJK text in many real tokenizers."""

    def token_offsets(self, text: str) -> list[tuple[int, int]]:
        return [(i, i + 1) for i, ch in enumerate(text) if not ch.isspace()]


def chunker(size: int = 10, overlap: int = 2) -> Chunker:
    return Chunker(WordTokenizer(), chunk_size=size, overlap=overlap)


def chunk_text(text: str, size: int = 10, overlap: int = 2) -> list[Chunk]:
    return list(chunker(size, overlap).chunk_pages([(None, text)]))


def words(n: int, prefix: str = "w") -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


def assert_invariants(chunks: list[Chunk], text: str, size: int) -> None:
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    covered = [False] * len(text)
    for c in chunks:
        assert c.text == text[c.char_start : c.char_end]
        assert c.text.strip() == c.text and c.text
        assert 0 < c.token_count <= size
        for i in range(c.char_start, c.char_end):
            covered[i] = True
    # Every non-whitespace character belongs to at least one chunk.
    assert all(covered[i] for i, ch in enumerate(text) if not ch.isspace())
    # Starts strictly increase: no chunk repeats or is contained in its predecessor.
    starts = [c.char_start for c in chunks]
    ends = [c.char_end for c in chunks]
    assert starts == sorted(set(starts))
    assert ends == sorted(set(ends))


@pytest.mark.parametrize("text", ["", "   ", "\n\n\n", " \t "])
def test_empty_or_whitespace_text_gives_no_chunks(text: str) -> None:
    assert chunk_text(text) == []


def test_short_text_is_one_chunk() -> None:
    chunks = chunk_text("Hello world.")
    assert chunks == [
        Chunk(chunk_index=0, text="Hello world.", page_number=None, char_start=0,
              char_end=12, token_count=2)
    ]


def test_long_text_respects_size_and_covers_everything() -> None:
    text = words(500)
    chunks = chunk_text(text, size=50, overlap=10)
    assert len(chunks) > 10
    assert_invariants(chunks, text, 50)


def test_overlap_repeats_tail_of_previous_chunk() -> None:
    text = words(30)
    chunks = chunk_text(text, size=10, overlap=3)
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert prev.text.split()[-3:] == nxt.text.split()[:3]


def test_zero_overlap_produces_disjoint_chunks() -> None:
    text = words(30)
    chunks = chunk_text(text, size=10, overlap=0)
    assert [c.token_count for c in chunks] == [10, 10, 10]
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert prev.char_end < nxt.char_start


def test_paragraphs_that_fit_are_not_split() -> None:
    paragraphs = [words(6, "a"), words(6, "b"), words(6, "c")]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, size=10, overlap=0)
    assert [c.text for c in chunks] == paragraphs


def test_small_paragraphs_are_packed_together() -> None:
    text = "One two.\n\nThree four.\n\nFive six."
    chunks = chunk_text(text, size=10, overlap=0)
    assert [c.text for c in chunks] == [text]


def test_long_paragraph_splits_at_sentence_boundaries() -> None:
    sentences = [f"{words(4, p)}." for p in "abcdef"]
    text = " ".join(sentences)
    chunks = chunk_text(text, size=10, overlap=0)
    # Two 4-word sentences fit in 10 tokens; three do not.
    assert [c.text for c in chunks] == [
        " ".join(sentences[0:2]),
        " ".join(sentences[2:4]),
        " ".join(sentences[4:6]),
    ]


def test_overlap_uses_finer_boundary_when_sentence_is_too_big() -> None:
    sentences = [f"{words(4, p)}." for p in "abcdef"]
    text = " ".join(sentences)
    chunks = chunk_text(text, size=10, overlap=2)
    # The tail of "b0 b1 b2 b3." is carried word by word.
    assert chunks[1].text.startswith("b2 b3.")
    assert_invariants(chunks, text, 10)


def test_text_without_spaces_is_hard_cut_on_token_boundaries() -> None:
    text = "数据" * 50  # 100 characters, one token each, no separators at all
    chunks = list(Chunker(CharTokenizer(), chunk_size=30, overlap=5).chunk_pages([(None, text)]))
    assert all(c.token_count <= 30 for c in chunks)
    assert chunks[0].char_start == 0 and chunks[-1].char_end == len(text)
    assert_invariants(chunks, text, 30)


@pytest.mark.parametrize(
    "sentence",
    [
        "The contract was signed in Almaty on 5 March.",
        "Договор был подписан в Алматы пятого марта.",
        "Келісімшарт наурыздың бесінде Алматыда қол қойылды, өңірлік ұйым үшін маңызды.",
        "Smart quotes «так» and “so” — mixed: English, русский, қазақша.",
    ],
)
def test_multilingual_text_keeps_exact_offsets(sentence: str) -> None:
    text = "\n\n".join([sentence] * 12)
    chunks = chunk_text(text, size=20, overlap=4)
    assert len(chunks) > 1
    assert_invariants(chunks, text, 20)


def test_kazakh_letters_survive_intact() -> None:
    text = "Әліпби: ә ғ қ ң ө ұ ү һ і. " * 20
    chunks = chunk_text(text.strip(), size=15, overlap=3)
    joined = "".join(c.text for c in chunks)
    for letter in "әғқңөұүһі":
        assert letter in joined


def test_sentence_split_keeps_closing_quote() -> None:
    text = f"«{words(8)}.» {words(8, 'x')}"
    chunks = chunk_text(text, size=10, overlap=0)
    assert chunks[0].text.endswith(".»")


def test_pages_are_never_merged_and_indices_continue() -> None:
    pages = [(1, words(15, "p")), (2, ""), (3, words(5, "q"))]
    chunks = list(chunker(10, 2).chunk_pages(pages))
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert [c.page_number for c in chunks] == [1, 1, 3]
    for c in chunks:
        page_text = dict(pages)[c.page_number]
        assert page_text[c.char_start : c.char_end] == c.text


def test_chunking_is_deterministic() -> None:
    text = "\n\n".join(words(n, f"p{n}_") for n in (3, 17, 8, 25, 1))
    assert chunk_text(text, 12, 4) == chunk_text(text, 12, 4)


def test_single_word_longer_than_overlap_still_progresses() -> None:
    # Each chunk is one token; overlap can never fit, so chunks just advance.
    chunks = chunk_text("alpha beta gamma", size=1, overlap=0)
    assert [c.text for c in chunks] == ["alpha", "beta", "gamma"]


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (10, 10), (10, 11), (10, -1)])
def test_invalid_configuration_is_rejected(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        Chunker(WordTokenizer(), chunk_size=size, overlap=overlap)


@pytest.mark.parametrize("seed", range(25))
def test_random_texts_keep_all_invariants(seed: int) -> None:
    rng = random.Random(seed)
    vocab = ["a", "bb", "слово", "сөз", "word.", "end!", "«q»", "x" * 40, "?", "…"]
    seps = [" ", " ", " ", "\n", "\n\n"]
    text = "".join(rng.choice(vocab) + rng.choice(seps) for _ in range(rng.randint(0, 400)))
    size = rng.randint(1, 40)
    overlap = rng.randint(0, size - 1)
    chunks = chunk_text(text, size, overlap)
    assert_invariants(chunks, text, size)


@pytest.mark.parametrize(("size", "overlap"), [(2, 1), (5, 4), (7, 3), (64, 63)])
def test_large_overlap_never_loops_or_duplicates(size: int, overlap: int) -> None:
    text = "\n\n".join(f"{words(n, 's')}." for n in (1, 9, 2, 30, 4, 4, 70))
    chunks = chunk_text(text, size, overlap)
    assert_invariants(chunks, text, size)
