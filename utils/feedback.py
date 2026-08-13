"""What the user told us, and what the app learned from it.

Two files under data/, both surviving a restart (see config.PERSISTENT_NAMES):

    feedback.jsonl   append-only, one JSON record per click, never rewritten
    learned.json     the model derived from it, versioned

The split matters.  The .jsonl is EVIDENCE - it is what actually happened,
it is never edited, and every learned model can be rebuilt from it.  The
.json is an OPINION derived from that evidence, and opinions get rolled back
(see rollback()).  Keeping the two apart is what makes "this update made
things worse, undo it" a one-line operation instead of an archaeology
project.

What is learned, and what is deliberately not:

  * NOT exact text.  `if text == last_rejected: reject` is a lookup table,
    not learning, and it generalises to nothing.
  * Tokens - words and Gujarati fragments - weighted by how lopsidedly they
    appear in rejected versus confirmed notices.  A word the user rejects
    five times and never confirms is evidence; a word that shows up in both
    is noise and scores near zero.

The guard rails are the point:

  * One click never makes a rule (MIN_SUPPORT).
  * The model can only DEMOTE something already uncertain, and can never
    veto a strong template match - see should_demote().  A learning system
    that can delete a confident detection is a recall bug waiting for a
    user to click the wrong button once.

Self-check:  python -m notice_extractor.utils.feedback
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

FEEDBACK_FILENAME = "feedback.jsonl"
LEARNED_FILENAME = "learned.json"

#: A token must appear in this many rejected notices before it counts at all.
#: One angry click on a legitimate notice must not teach the app anything.
MIN_SUPPORT = 3
#: ...and it must be at least this lopsided (negatives vs positives).
MIN_RATIO = 3.0
#: Tokens shorter than this match too much to mean anything - the same rule
#: utils.search applies to query tokens, for the same reason.
MIN_TOKEN_LEN = 4
#: How sure the learned model has to be before it will demote anything.
DEMOTE_SCORE = 1.5
#: A detection this confident is never demoted, whatever the model thinks.
#: The template matched a real newspaper heading; a bag of words does not
#: get to overrule that.
PROTECT_CONFIDENCE = 88


def _path(name: str) -> str:
    from .. import config
    return config.session_file(name)


def image_fingerprint(image_bgr) -> str:
    """A short, stable id for a crop, so a feedback record can be tied back
    to the picture the user was actually looking at."""
    try:
        import cv2
        small = cv2.resize(image_bgr, (32, 32))
        return hashlib.sha1(small.tobytes()).hexdigest()[:16]
    except Exception:
        return ""


# --- recording ----------------------------------------------------------------

def record(result, verdict: str, origin: str = "results") -> dict:
    """Append one feedback record and return it.

    `verdict` is "positive" or "negative"; `origin` is "results" (the normal
    list, where the only button is Not Related) or "review" (the Not Sure
    queue, where both buttons live).  Origin is kept because the two are not
    equally strong evidence: rejecting something the app was already unsure
    about says less than rejecting something it was confident of."""
    from . import search as search_util
    from .. import core

    text = getattr(result, "ocr_text", "") or ""
    entry = {
        "id": f"{int(time.time() * 1000):x}-{getattr(result, 'result_id', 0)}",
        "item_id": getattr(result, "result_id", 0),
        "feedback": verdict,
        "origin": origin,
        "source": getattr(result, "newspaper", ""),
        "edition": getattr(result, "edition", ""),
        "date": getattr(result, "issue_date", ""),
        "page": getattr(result, "page_number", 0),
        "category": "public_notice",
        "subtype": getattr(result, "notice_type", "") or "",
        "ocr_text": text[:4000],
        "normalized_text": core.normalize_ocr_text(text)[:4000],
        "image_hash": image_fingerprint(getattr(result, "image_bgr", None)),
        "classifier_score": getattr(result, "confidence", 0),
        "classifier_method": getattr(result, "method", ""),
        "classifier_version": learned_version(),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        with open(_path(FEEDBACK_FILENAME), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass          # a lost vote must never take the click down with it
    return entry


def load_records() -> List[dict]:
    """Every feedback record.  A corrupt line is skipped, not fatal: this
    file is appended to while the app is running, and a half-written last
    line after a power cut must not cost the user their whole history."""
    path = _path(FEEDBACK_FILENAME)
    records: List[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("feedback"):
                    records.append(entry)
    except OSError:
        return []
    return records


def counts() -> Tuple[int, int]:
    """(positive, negative) - what the UI shows next to the review queue."""
    records = load_records()
    positive = sum(1 for r in records if r["feedback"] == "positive")
    return positive, len(records) - positive


# --- learning -----------------------------------------------------------------

def _tokens(normalized: str) -> set:
    """Overlapping fragments of the normalised text.

    Gujarati normalises to one unbroken string (whitespace is stripped, by
    design - see utils.search), so there are no "words" to split on.  Fixed
    length shingles give the same job to both scripts: a repeated phrase
    shows up as a run of shared fragments whatever the language."""
    if not normalized:
        return set()
    size = 6
    if len(normalized) <= size:
        return {normalized} if len(normalized) >= MIN_TOKEN_LEN else set()
    # step 3: overlapping enough to survive an OCR slip, sparse enough that
    # a long notice does not produce thousands of tokens.
    return {normalized[i:i + size]
            for i in range(0, len(normalized) - size + 1, 3)}


def build_model(records: Optional[Sequence[dict]] = None) -> dict:
    """Derive a model from the evidence.  Pure - no I/O, so it can be
    measured against held-out records before anything is saved."""
    records = load_records() if records is None else records
    negative_docs = [r for r in records if r["feedback"] == "negative"]
    positive_docs = [r for r in records if r["feedback"] == "positive"]

    neg_count: Dict[str, int] = {}
    pos_count: Dict[str, int] = {}
    for bucket, docs in ((neg_count, negative_docs), (pos_count, positive_docs)):
        for entry in docs:
            for token in _tokens(entry.get("normalized_text", "")):
                bucket[token] = bucket.get(token, 0) + 1

    weights: Dict[str, float] = {}
    for token, negatives in neg_count.items():
        if negatives < MIN_SUPPORT:
            continue                     # one or two clicks are not a rule
        positives = pos_count.get(token, 0)
        if positives and negatives / float(positives) < MIN_RATIO:
            continue                     # appears in both - it means nothing
        # Log so the tenth rejection adds less than the third: evidence
        # accumulates, it does not multiply.
        weights[token] = round(math.log1p(negatives - positives) , 4)

    return {
        "version": 1,
        "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "positive_examples": len(positive_docs),
        "negative_examples": len(negative_docs),
        "weights": weights,
        "metrics": {},
    }


def load_model() -> dict:
    try:
        with open(_path(LEARNED_FILENAME), encoding="utf-8") as handle:
            model = json.load(handle)
    except (OSError, ValueError):
        return {"version": 0, "weights": {}, "positive_examples": 0,
                "negative_examples": 0, "history": []}
    if not isinstance(model, dict) or "weights" not in model:
        return {"version": 0, "weights": {}, "positive_examples": 0,
                "negative_examples": 0, "history": []}
    return model


def learned_version() -> int:
    return int(load_model().get("version", 0))


def save_model(model: dict) -> dict:
    """Write a new model, keeping the previous one so it can be rolled back.

    History is capped: the point is being able to undo the last bad update,
    not to keep every model ever built."""
    current = load_model()
    history = list(current.get("history", []))
    if current.get("weights"):
        snapshot = dict(current)
        snapshot.pop("history", None)
        history.append(snapshot)
    model = dict(model)
    model["version"] = int(current.get("version", 0)) + 1
    model["history"] = history[-5:]
    path = _path(LEARNED_FILENAME)
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(model, handle, ensure_ascii=False, indent=1)
        os.replace(temporary, path)
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass
    return model


def rollback() -> Optional[dict]:
    """Go back to the previous model.  Returns it, or None if there is none.

    This exists because a learning update is a change like any other, and
    the rule in this project is that a change that makes things worse gets
    reverted rather than argued with."""
    current = load_model()
    history = list(current.get("history", []))
    if not history:
        return None
    previous = history.pop()
    previous["history"] = history
    previous["version"] = int(current.get("version", 0)) + 1
    previous["rolled_back_from"] = current.get("version")
    path = _path(LEARNED_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(previous, handle, ensure_ascii=False, indent=1)
    except OSError:
        return None
    return previous


def relearn() -> dict:
    """Rebuild from all evidence and save.  Called after each new record."""
    return save_model(build_model())


# --- applying it --------------------------------------------------------------

def score(normalized_text: str, model: Optional[dict] = None) -> float:
    """How much learned NEGATIVE evidence this text carries.  0.0 = none."""
    model = load_model() if model is None else model
    weights = model.get("weights") or {}
    if not weights or not normalized_text:
        return 0.0
    total = 0.0
    for token in _tokens(normalized_text):
        total += weights.get(token, 0.0)
    return round(total, 4)


def should_demote(result, model: Optional[dict] = None) -> bool:
    """Has this notice earned its way out of the results?

    Two hard limits, both there to protect recall:

      * a confident detection is never demoted (PROTECT_CONFIDENCE) - the
        template matched a real printed heading, and no amount of word
        overlap outranks that;
      * a crop nobody has read yet is never demoted - "we do not know" must
        not read as "reject"."""
    if not getattr(result, "ocr_done", False):
        return False
    if getattr(result, "confidence", 0) >= PROTECT_CONFIDENCE:
        return False
    model = load_model() if model is None else model
    return score(getattr(result, "normalized_ocr", "") or "", model) \
        >= DEMOTE_SCORE


# --- self-check ---------------------------------------------------------------

class _Result:
    def __init__(self, text, confidence=70, done=True):
        from .. import core
        self.ocr_text = text
        self.normalized_ocr = core.normalize_ocr_text(text)
        self.confidence = confidence
        self.ocr_done = done
        self.result_id = 1
        self.newspaper = "Gujarat Samachar"
        self.page_number = 4
        self.edition = "ahmedabad"
        self.issue_date = "2026-08-12"
        self.notice_type = "notice"
        self.method = "box+template"
        self.image_bgr = None


def demo() -> None:
    from .. import config

    path = _path(FEEDBACK_FILENAME)
    learned = _path(LEARNED_FILENAME)
    for f in (path, learned):
        if os.path.exists(f):
            os.remove(f)

    advert = ("ASIAD CIRCUS tickets available on bookmyshow "
              "roj 3 show evening")
    notice = ("આથી જાહેર જનતાને જણાવવાનું કે સદરહુ મિલકત અંગે "
              "કોઈપણ પ્રકારનો હક્ક હિસ્સો")

    # One rejection teaches nothing.
    record(_Result(advert), "negative")
    model = build_model()
    assert not model["weights"], "a single click became a rule"

    # Repeated rejections of the same KIND do.
    for _ in range(MIN_SUPPORT):
        record(_Result(advert + " special offer"), "negative")
    model = build_model()
    assert model["weights"], "repeated rejections taught nothing"
    assert model["negative_examples"] == MIN_SUPPORT + 1

    # It generalises: a DIFFERENT advert with shared wording scores too.
    from .. import core
    similar = core.normalize_ocr_text("ASIAD CIRCUS tickets available today")
    assert score(similar, model) > 0, "learned nothing transferable"
    # ...while a real notice does not.
    assert score(core.normalize_ocr_text(notice), model) == 0.0, \
        "a real notice picked up negative evidence"

    # A token that appears in BOTH is not evidence.
    for _ in range(4):
        record(_Result(notice), "positive")
    model = build_model()
    assert score(core.normalize_ocr_text(notice), model) == 0.0
    assert model["positive_examples"] == 4

    # Recall guard: a confident detection is never demoted.
    loud = _Result(advert + " special offer", confidence=95)
    assert not should_demote(loud, model), \
        "a confident detection was demoted by word overlap"
    quiet = _Result(advert + " special offer", confidence=70)
    save_model(model)
    assert should_demote(quiet, load_model()), \
        "a low-confidence learned negative was not demoted"
    # An unread crop is never demoted.
    unread = _Result(advert + " special offer", confidence=70, done=False)
    assert not should_demote(unread, load_model())

    # Versioning and rollback.
    first = load_model()
    assert first["version"] >= 1
    save_model(build_model())
    assert load_model()["version"] == first["version"] + 1
    restored = rollback()
    assert restored is not None and restored["rolled_back_from"] is not None

    positive, negative = counts()
    assert positive == 4 and negative == MIN_SUPPORT + 1, (positive, negative)

    for f in (path, learned):
        if os.path.exists(f):
            os.remove(f)
    print("feedback self-check OK")


if __name__ == "__main__":
    demo()
