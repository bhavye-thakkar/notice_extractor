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

The recent-search list lives here too, because "are these two queries the
same?" is a matching question and the answer has to agree with the search
itself - "PUBLIC   NOTICE" and "public notice" find the same notices, so the
history must not hold both.

Self-check:  python -m notice_extractor.utils.search
"""

from __future__ import annotations

import difflib
import json
import os
import re
from typing import List, Sequence, Tuple

#: Similarity a fuzzy comparison needs to count as a hit.
FUZZY_MATCH_RATIO = 0.80
#: Tokens shorter than this are matched exactly - fuzzy-matching a two-letter
#: word matches half the page.
FUZZY_MIN_TOKEN_LEN = 4

#: Recent searches, newest first.  Kept in data/ so it survives a restart -
#: config.clear_run_data() only removes the transient FOLDERS.
RECENT_FILENAME = "recent_searches.json"
#: The dropdown has to stay a glance, not a scroll.
RECENT_LIMIT = 12

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
    dropped/garbled matra that Gujarati OCR produces.

    This is the hottest pure-Python function in the app: detection calls it
    for every keyword against every candidate header strip, and pure Python
    holds the GIL, so its cost lands directly on the UI thread as a freeze.
    Profiling one page found 1932 calls turning into 101,822
    SequenceMatcher.ratio() calls.  Three exact optimisations - none of them
    changes a single returned value:

      * real_quick_ratio() and quick_ratio() are documented UPPER BOUNDS on
        ratio().  When the bound is already below the threshold the window
        cannot pass, so the O(n*m) ratio() is skipped.  A skipped window
        scores under min_ratio, and this returns 0.0 for anything under
        min_ratio anyway, so no returned value moves.
      * Identical windows are only scored once.

    NOT done: swapping the needle into the matcher's `b` slot to stop it
    re-indexing per window.  That is much faster and looks safe, but
    difflib.ratio() is not symmetric - find_longest_match breaks ties toward
    the earlier match in `a` - and a randomised comparison found it changing
    2 results in 8400.  Exactness wins.
    """
    if not haystack or not needle:
        return 0.0
    if needle in haystack:
        return 1.0
    n = len(needle)
    if len(haystack) < max(3, int(n * min_ratio)):
        return 0.0
    best = 0.0
    matcher = difflib.SequenceMatcher(None, needle, "")
    seen: set = set()
    for width in (n, n + 1, n - 1):
        if width < 3:
            continue
        for start in range(0, max(1, len(haystack) - width + 1)):
            window = haystack[start:start + width]
            if window in seen:
                continue
            seen.add(window)
            matcher.set_seq2(window)
            # Cheap upper bounds first: O(1), then O(n).
            if matcher.real_quick_ratio() < min_ratio or \
                    matcher.quick_ratio() < min_ratio:
                continue
            ratio = matcher.ratio()
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


# --- recent searches ----------------------------------------------------------

def same_query(a: str, b: str) -> bool:
    """Are these the same search?  Case- and spacing-insensitive.

    NOT normalize_ocr_text(): that strips whitespace entirely, which would
    fold "public notice" into "publicnotice" - and those are different
    searches to the user even though the matcher treats a glued OCR word as
    a hit for both."""
    return " ".join((a or "").split()).casefold() == \
           " ".join((b or "").split()).casefold()


def _recent_path() -> str:
    # Late import: this module is otherwise pure (no paths, no I/O), which is
    # what lets `python -m notice_extractor.utils.search` self-check run.
    from .. import config
    return config.session_file(RECENT_FILENAME)


def load_recent() -> List[str]:
    """The saved searches, newest first.  A missing or corrupt file is an
    empty history, never an error: nobody should lose a search UI because a
    convenience file got truncated by a power cut."""
    try:
        with open(_recent_path(), encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(saved, list):
        return []
    return [item for item in saved
            if isinstance(item, str) and item.strip()][:RECENT_LIMIT]


def _save_recent(queries: List[str]) -> List[str]:
    """Write the list, atomically.  A half-written file would read back as
    corrupt and silently wipe the history."""
    path = _recent_path()
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(queries, handle, ensure_ascii=False, indent=1)
        os.replace(temporary, path)
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass
    return queries


def remember_search(query: str) -> List[str]:
    """Record a search and return the new history, newest first.

    Searching the same thing again moves it to the top and keeps the NEWEST
    spelling, rather than adding a second row - a history that fills up with
    "public notice" three times is not a history."""
    query = " ".join((query or "").split())
    if not query:
        return load_recent()
    kept = [item for item in load_recent() if not same_query(item, query)]
    return _save_recent([query] + kept[:RECENT_LIMIT - 1])


def forget_search(query: str) -> List[str]:
    """Drop one entry (the per-item Remove)."""
    return _save_recent([item for item in load_recent()
                         if not same_query(item, query)])


def clear_recent() -> List[str]:
    """Drop the whole history (Clear search history)."""
    return _save_recent([])


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

    # જાહેર ચેતવણી must behave exactly like જાહેર નોટિસ - same category, so
    # the same search rules, including OCR damage and split lines.
    warn = "જાહેર ચેતવણી આથી જાહેર જનતાને જણાવવાનું કે"
    assert match_query(warn, "જાહેર ચેતવણી")
    assert match_query("જાહેર   ચેતવણી", "જાહેર ચેતવણી")     # extra spaces
    assert match_query("જાહેર\nચેતવણી", "જાહેર ચેતવણી")      # split over lines
    assert match_query("જાહેરચેતવણી", "જાહેર ચેતવણી")        # words joined
    assert not match_query(warn, "જાહેર હરાજી")

    # -- recent searches -------------------------------------------------------
    # Same query, different spelling: one row, newest form, newest first.
    assert same_query("public notice", "PUBLIC   Notice")
    assert not same_query("public notice", "publicnotice")

    kept = clear_recent()
    assert kept == []
    remember_search("public notice")
    remember_search("જાહેર ચેતવણી")
    assert load_recent() == ["જાહેર ચેતવણી", "public notice"]
    remember_search("PUBLIC   NOTICE")            # the duplicate case
    assert load_recent() == ["PUBLIC NOTICE", "જાહેર ચેતવણી"], load_recent()
    assert forget_search("જાહેર ચેતવણી") == ["PUBLIC NOTICE"]
    remember_search("   ")                        # empty query saves nothing
    assert load_recent() == ["PUBLIC NOTICE"]
    for n in range(RECENT_LIMIT + 5):             # the cap holds
        remember_search(f"query {n}")
    assert len(load_recent()) == RECENT_LIMIT
    assert load_recent()[0] == f"query {RECENT_LIMIT + 4}"
    clear_recent()
    assert load_recent() == []
    print("search self-check OK")


if __name__ == "__main__":
    demo()
