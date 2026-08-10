"""Text matching for the notices - normalisation, fuzzy compare, and the
multi-word "Find text" search.

Why this is its own module: the search runs over OCR output, and OCR output is
never clean.  Gujarati matras get dropped, words get split in the middle and
spacing is unreliable, so a plain `query in text` answers the wrong question.
Everything that has to cope with that lives here, and both the detection
keywords (core.py) and the gallery search (ui) use the same rules.

The search itself is TOKEN based: every word of the query must appear
somewhere in the notice, in any order.  That is what makes "public notice"
work - the old code normalised the query to "publicnotice" (normalisation
strips whitespace) and then looked for that one blob inside single OCR words,
which no real word ever contains.

Self-check:  python -m notice_extractor.utils.search
"""

from __future__ import annotations

import difflib
import re
from typing import List, Sequence, Tuple

#: Similarity a fuzzy comparison needs to count as a hit.
FUZZY_MATCH_RATIO = 0.80
#: Tokens shorter than this are matched exactly - fuzzy-matching a two-letter
#: word matches half the page.
FUZZY_MIN_TOKEN_LEN = 4

_ZERO_WIDTH_RE = re.compile("[​‌‍﻿]")
_NON_WORD_RE = re.compile(r"[^\w઀-૿]+", re.UNICODE)

Box = Tuple[int, int, int, int]


def normalize_ocr_text(text: str) -> str:
    """Collapse OCR output for keyword search: drop zero-width characters,
    punctuation and ALL whitespace; casefold Latin."""
    text = _ZERO_WIDTH_RE.sub("", text or "")
    text = _NON_WORD_RE.sub("", text)
    return text.replace("_", "").casefold()


def fuzzy_contains(haystack: str, needle: str,
                   min_ratio: float = FUZZY_MATCH_RATIO) -> float:
    """Best similarity of `needle` against any window of `haystack`.
    Returns the best ratio if >= min_ratio, else 0.0.  Tolerates the odd
    dropped/garbled matra that Gujarati OCR produces."""
    if not haystack or not needle:
        return 0.0
    if needle in haystack:
        return 1.0
    n = len(needle)
    if len(haystack) < max(3, int(n * min_ratio)):
        return 0.0
    best = 0.0
    # Slide a window of similar length across the haystack.
    for width in (n, n + 1, n - 1):
        if width < 3:
            continue
        for start in range(0, max(1, len(haystack) - width + 1)):
            ratio = difflib.SequenceMatcher(
                None, needle, haystack[start:start + width]).ratio()
            if ratio > best:
                best = ratio
    return best if best >= min_ratio else 0.0


def tokenize_query(query: str) -> List[str]:
    """The query as normalised words.  "Public  Notice!" -> ["public",
    "notice"]; Gujarati works the same way."""
    return [token for token in (normalize_ocr_text(word)
                                for word in (query or "").split()) if token]


def _token_present(token: str, haystack: str, fuzzy: bool) -> bool:
    if token in haystack:
        return True
    if not fuzzy or len(token) < FUZZY_MIN_TOKEN_LEN:
        return False
    return fuzzy_contains(haystack, token) > 0.0


def match_query(text: str, query: str, fuzzy: bool = True) -> bool:
    """Does `text` contain EVERY word of `query` (any order)?

    This is the whole search contract - one function, so the GUI, a future
    CLI filter and the tests cannot disagree."""
    tokens = tokenize_query(query)
    if not tokens:
        return False
    haystack = normalize_ocr_text(text)
    return all(_token_present(token, haystack, fuzzy) for token in tokens)


def search_notice(words: Sequence, full_text: str, query: str,
                  fuzzy: bool = True) -> Tuple[bool, List[Box]]:
    """Search one notice.  Returns (matched?, boxes to highlight).

    `words` are OCR words with .text/.x/.y/.w/.h (core.OcrWord).  A notice can
    match without any box: OCR sometimes glues the phrase into one blob whose
    pieces no longer line up with the query words.  The caller counts the
    match from the flag, never from `len(boxes)`."""
    tokens = tokenize_query(query)
    if not tokens:
        return False, []

    normalized_words = [(word, normalize_ocr_text(getattr(word, "text", "")))
                        for word in words]
    # Prefer the words' own text: `full_text` may be missing when a notice was
    # read by an engine that only returns lines.
    haystack = normalize_ocr_text(full_text) or "".join(
        text for _word, text in normalized_words)

    if not all(_token_present(token, haystack, fuzzy) for token in tokens):
        return False, []

    boxes: List[Box] = []
    for word, text in normalized_words:
        if not text:
            continue
        hit = any(token in text or text in token for token in tokens)
        if not hit and fuzzy:
            hit = any(len(token) >= FUZZY_MIN_TOKEN_LEN
                      and fuzzy_contains(text, token) > 0.0
                      for token in tokens)
        if hit:
            boxes.append((word.x, word.y, word.w, word.h))
    return True, boxes


# --- self-check ---------------------------------------------------------------

class _Word:                      # tiny stand-in for core.OcrWord
    def __init__(self, text, x=0, y=0, w=10, h=10):
        self.text, self.x, self.y, self.w, self.h = text, x, y, w, h


def demo() -> None:
    text = "PUBLIC NOTICE is hereby given that the plot"
    words = [_Word(w, i * 10) for i, w in enumerate(text.split())]

    # The bug this module exists to fix: multi-word queries.
    assert match_query(text, "public notice")
    assert match_query(text, "notice public")          # order-free
    assert match_query(text, "  Public   NOTICE! ")    # spacing / punctuation
    assert match_query(text, "public")
    assert not match_query(text, "public auction")     # one word missing
    assert not match_query(text, "   ")                # empty query

    matched, boxes = search_notice(words, text, "public notice")
    assert matched and len(boxes) == 2, boxes
    assert not search_notice(words, text, "public auction")[0]

    # Gujarati: the same phrase, and a matra dropped by OCR must still match.
    guj = "જાહેર નોટિસ આથી જણાવવામાં આવે છે"
    guj_words = [_Word(w, i * 10) for i, w in enumerate(guj.split())]
    assert match_query(guj, "જાહેર નોટિસ")
    assert search_notice(guj_words, guj, "જાહેર નોટિસ")[1]
    assert match_query("જાહર નોટિસ આથી", "જાહેર નોટિસ")   # OCR dropped a matra
    assert not match_query(guj, "જાહેર હરાજી")

    # A notice whose words OCR glued together still counts as a match.
    glued = [_Word("PUBLICNOTICE")]
    assert search_notice(glued, "PUBLICNOTICE", "public notice")[0]
    print("search self-check OK")


if __name__ == "__main__":
    demo()
