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
        for start in _candidate_starts(haystack, needle, width, min_ratio):
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


def _candidate_starts(haystack: str, needle: str, width: int,
                      min_ratio: float):
    """Window starts whose quick_ratio() bound can still reach `min_ratio`
    - an EXACT prefilter, computed in O(1) per window.

    SequenceMatcher.quick_ratio() is 2*|multiset(needle) & multiset(window)|
    / (n + width), an upper bound on ratio(); fuzzy_contains already skips
    every window under that bound.  Here the same intersection size is
    kept incrementally as the window slides (a character entering counts
    when the window holds fewer of it than the needle; one leaving uncounts
    on the same rule), so the bound costs one dict step per position
    instead of a fresh SequenceMatcher pass over the window.  Only windows
    passing it are handed to difflib - the identical set fuzzy_contains
    would have scored, so no returned value moves (see demo()).  Measured
    on a Sandesh board page, keyword matching was 44 s of a 59 s detect."""
    n = len(needle)
    length = len(haystack)
    total = max(1, length - width + 1)
    if width > length:
        return range(total)
    need: dict = {}
    for ch in needle:
        need[ch] = need.get(ch, 0) + 1
    have: dict = {}
    inter = 0
    for ch in haystack[:width]:
        c = have.get(ch, 0)
        if c < need.get(ch, 0):
            inter += 1
        have[ch] = c + 1
    denominator = n + width
    starts = []
    if 2.0 * inter / denominator >= min_ratio:
        starts.append(0)
    for start in range(1, total):
        out = haystack[start - 1]
        c = have[out] - 1
        have[out] = c
        if c < need.get(out, 0):
            inter -= 1
        new = haystack[start + width - 1]
        c = have.get(new, 0)
        if c < need.get(new, 0):
            inter += 1
        have[new] = c + 1
        if 2.0 * inter / denominator >= min_ratio:
            starts.append(start)
    return starts


#: Gujarati signs OCR (and typists) swap freely: long/short vowel signs and
#: the two nasal marks.  Folded for the SEARCH only - "નોટીસ" typed must find
#: "નોટિસ" printed and vice versa, and neither the OCR nor the user is
#: consistent about which one it is.  Detection keywords are not folded:
#: they carry every spelling explicitly and their thresholds were measured
#: on the unfolded text.
_GUJ_FOLD = str.maketrans({
    "ી": "િ", "ૂ": "ુ", "ૈ": "ે", "ૌ": "ો", "ઁ": "ં", "ઈ": "ઇ", "ઊ": "ઉ",
    "ઐ": "એ", "ઔ": "ઓ", "ૅ": "ે", "ૉ": "ો", "ઍ": "એ", "ઑ": "ઓ",
    "ઃ": "", "઼": "", "્": "્",
})


def fold_for_search(normalized: str) -> str:
    """Search-side folding of already-normalised text (see _GUJ_FOLD)."""
    return normalized.translate(_GUJ_FOLD)


# --- Gujarati-aware approximate matching --------------------------------------
# difflib's ratio treats every character slip the same, and Gujarati OCR does
# not slip evenly: it drops anusvara and matras, and it swaps consonants that
# LOOK alike (ખ/ષ, ડ/ઙ, ભ/મ, ન/ત ...).  A user typing સાણંદ or ચેખલા must find
# the notice whose OCR reads સાણદ or ચેષલા.  So Gujarati tokens are matched
# with a weighted edit distance where those slips cost half a point, against
# any substring of the text (Sellers' approximate-substring DP), with an
# error budget that grows with the word.  Latin tokens keep difflib.

_GUJ_RE = re.compile("[઀-૿]")
#: Signs OCR drops or garbles most - a slip here is half an error.
_GUJ_SIGNS = frozenset("ાિીુૂૃૄૅેૈૉોૌંઁઃ્")
#: Look-alike consonant classes.  A swap INSIDE a class is half an error.
_GUJ_CONFUSABLE_CLASSES = ("ખષ", "શસષ", "ડઙ", "ઘધ", "ભમ", "યથ", "વચ", "કફ",
                           "ટડ", "ળલ", "નત", "જઝ", "બલ", "ગમ", "ઠદ", "પય",
                           "રસ", "હઠ", "છઈ", "ઈઇ", "ઉઊ")
_GUJ_CLASS: dict = {}
for _index, _group in enumerate(_GUJ_CONFUSABLE_CLASSES):
    for _ch in _group:
        _GUJ_CLASS.setdefault(_ch, set()).add(_index)


def has_gujarati(text: str) -> bool:
    return bool(_GUJ_RE.search(text or ""))


def _guj_sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if a in _GUJ_SIGNS and b in _GUJ_SIGNS:
        return 0.5                       # one matra read as another
    if _GUJ_CLASS.get(a) and _GUJ_CLASS.get(b) and \
            _GUJ_CLASS[a] & _GUJ_CLASS[b]:
        return 0.5                       # look-alike consonants
    return 1.0


def _guj_indel_cost(ch: str) -> float:
    return 0.5 if ch in _GUJ_SIGNS else 1.0


def guj_error_budget(length: int) -> float:
    """How much weighted error a Gujarati token of `length` code points may
    carry and still match.  Short words get one matra slip; long words
    about one full error per five characters."""
    if length < 3:
        return 0.0
    if length < 4:
        return 0.5
    if length < 6:
        return 1.0
    if length < 9:
        return 1.5
    if length < 12:
        return 2.0
    return length / 5.0


def _guj_best_cost(segment: str, needle: str, budget: float) -> float:
    """Minimum weighted edit cost of `needle` against ANY substring of
    `segment` (Sellers: free start anywhere, free end anywhere)."""
    m = len(needle)
    prev = [0.0] * (len(segment) + 1)            # row 0: empty needle
    for i in range(1, m + 1):
        nch = needle[i - 1]
        cur = [prev[0] + _guj_indel_cost(nch)]   # needle char unmatched
        row_min = cur[0]
        for j in range(1, len(segment) + 1):
            sch = segment[j - 1]
            cost = min(prev[j - 1] + _guj_sub_cost(nch, sch),
                       prev[j] + _guj_indel_cost(nch),
                       cur[j - 1] + _guj_indel_cost(sch))
            cur.append(cost)
            if cost < row_min:
                row_min = cost
        if row_min > budget:
            return row_min                       # cannot recover
        prev = cur
    return min(prev)


def guj_fuzzy_contains(haystack: str, needle: str) -> float:
    """Gujarati-aware approximate 'needle in haystack'.  Returns a
    similarity in (0, 1] when the best substring is within the error budget,
    else 0.0.  Both strings must already be normalised and folded."""
    if not haystack or not needle:
        return 0.0
    if needle in haystack:
        return 1.0
    m = len(needle)
    budget = guj_error_budget(m)
    if budget <= 0:
        return 0.0
    if len(haystack) <= m + 6:
        cost = _guj_best_cost(haystack, needle, budget)
        return round(1.0 - cost / m, 4) if cost <= budget else 0.0
    # Long text: only look around windows that share enough characters
    # with the needle (the same exact bound fuzzy_contains uses), then run
    # the DP on a small segment round each.  Segments overlapping are
    # merged so no window is scored twice.
    starts = _candidate_starts(haystack, needle, m, 0.55)
    if not starts:
        return 0.0
    best = None
    seg_lo = seg_hi = -1
    for start in starts:
        lo, hi = max(0, start - 3), min(len(haystack), start + m + 3)
        if lo <= seg_hi:                    # extend the current segment
            seg_hi = max(seg_hi, hi)
            continue
        if seg_lo >= 0:
            cost = _guj_best_cost(haystack[seg_lo:seg_hi], needle, budget)
            if best is None or cost < best:
                best = cost
        seg_lo, seg_hi = lo, hi
    if seg_lo >= 0:
        cost = _guj_best_cost(haystack[seg_lo:seg_hi], needle, budget)
        if best is None or cost < best:
            best = cost
    if best is None or best > budget:
        return 0.0
    return round(1.0 - best / m, 4)


def tokenize_query(query: str) -> List[str]:
    """The query as normalised, search-folded words.  "Public  Notice!" ->
    ["public", "notice"]; Gujarati works the same way."""
    return [token for token in (fold_for_search(normalize_ocr_text(word))
                                for word in (query or "").split()) if token]


def _token_present(token: str, haystack: str, fuzzy: bool) -> bool:
    if token in haystack:
        return True
    if not fuzzy:
        return False
    if has_gujarati(token):
        return guj_fuzzy_contains(haystack, token) > 0.0
    if len(token) < FUZZY_MIN_TOKEN_LEN:
        return False
    return fuzzy_contains(haystack, token) > 0.0


def _word_hits(text: str, token: str, fuzzy: bool) -> bool:
    """Does one OCR word carry `token`?  Used to choose the boxes to draw."""
    if not text:
        return False
    if token in text or (len(text) >= 2 and text in token):
        return True
    if not fuzzy:
        return False
    if has_gujarati(token):
        # The word may be a fragment of the token (OCR split it) or carry
        # it with a slip: score the shorter against the longer.
        short, long_ = (text, token) if len(text) < len(token) else \
            (token, text)
        if len(short) < 3:
            return False
        return guj_fuzzy_contains(long_, short) > 0.0
    return len(token) >= FUZZY_MIN_TOKEN_LEN and \
        fuzzy_contains(text, token) > 0.0


def match_query(text: str, query: str, fuzzy: bool = True) -> bool:
    """Does `text` contain EVERY word of `query` (any order)?

    This is the whole search contract - one function, so the GUI, a future
    CLI filter and the tests cannot disagree."""
    tokens = tokenize_query(query)
    if not tokens:
        return False
    haystack = fold_for_search(normalize_ocr_text(text))
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

    normalized_words = [(word, fold_for_search(
        normalize_ocr_text(getattr(word, "text", "")))) for word in words]
    # Prefer the words' own text: `full_text` may be missing when a notice was
    # read by an engine that only returns lines.
    haystack = fold_for_search(normalize_ocr_text(full_text)) or "".join(
        text for _word, text in normalized_words)

    if not all(_token_present(token, haystack, fuzzy) for token in tokens):
        return False, []

    boxes: List[Box] = []
    for word, text in normalized_words:
        if any(_word_hits(text, token, fuzzy) for token in tokens):
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
    # Vowel length is folded for search: typed one way, printed the other.
    assert match_query("આ જાહેર નોટીસથી", "નોટિસ")
    assert match_query("આ જાહેર નોટિસથી", "નોટીસ")
    assert match_query("ચેતવણિ આપવામાં", "ચેતવણી")
    assert search_notice(guj_words, guj, "નોટીસ")[1], "folded box highlight"

    # Gujarati-aware fuzzy: what OCR actually does to place names.
    ocr = "મોજે ગામ સાણદ તાલુકો ચેષલા સીમમાં આવેલ જમીન સર્વે નંબર ૩૭"
    assert match_query(ocr, "સાણંદ"), "dropped anusvara"
    assert match_query(ocr, "ચેખલા"), "ખ read as ષ"
    assert match_query(ocr, "સાણંદ ચેખલા")
    assert match_query("...ધોળકા તાલુકાના...", "ધોળકા")
    assert match_query("ધોલકા તાલુકાના", "ધોળકા"), "ળ read as લ"
    assert not match_query(ocr, "વડોદરા"), "a different word must not match"
    assert not match_query(ocr, "ગામડું"), "short near-miss must not match"
    # A wrong consonant outside the look-alike classes is a full error:
    # one is allowed on a 5-char word, two are not.
    assert match_query("કલોલ", "કડોલ")
    assert not match_query("સાણંદ", "હાપંક")
    # Boxes: the OCR word carrying the slip is the one boxed.
    words = [_Word("મોજે", 0), _Word("ગામ", 40), _Word("સાણદ", 80),
             _Word("તાલુકો", 130)]
    hit, boxes = search_notice(words, "મોજે ગામ સાણદ તાલુકો", "સાણંદ")
    assert hit and boxes == [(80, 0, 10, 10)], boxes
    # Cost: a long crop, several tokens, must stay well under a frame.
    import time as _time
    long_text = ocr * 60
    t0 = _time.perf_counter()
    for _ in range(20):
        match_query(long_text, "સાણંદ ચેખલા વડોદરા")
    per = (_time.perf_counter() - t0) / 20
    assert per < 0.05, f"gujarati fuzzy too slow: {per*1000:.1f} ms"

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
