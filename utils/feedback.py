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
#: Region (layout-bucket) evidence needs more support than text evidence:
#: a bucket is far coarser than a token set, so three clicks that happen to
#: share a size class must not condemn every notice of that size.
REGION_MIN_SUPPORT = 4
#: What one fully-condemned region bucket contributes to the demote score.
#: Deliberately below DEMOTE_SCORE: layout alone never demotes, it can only
#: tip a candidate that already carries some learned text evidence.
REGION_WEIGHT_CAP = 0.9
LEARNING_HISTORY_FILENAME = "learning_history.jsonl"

# --- segmentation (Half Copy) -------------------------------------------------
# A SECOND, independent learner.  "Not Related" says the app returned the
# wrong thing; "Half Copy" says it returned the RIGHT thing badly cropped.
# Mixing them is the mistake this file exists to avoid: training a half crop
# as a negative would teach the classifier to reject the very notices it is
# supposed to find, which costs recall - the one thing that may never drop.
#
# So a half_crop record carries BOTH signals, on purpose:
#     relevance   -> positive   (this IS a notice)
#     segmentation-> negative   (this crop was incomplete)
SEGMENT_FILENAME = "segmentation.json"
#: How many half-crop reports of one layout bucket + direction before the
#: hint is applied to future runs.  Two, not one: a single click is an
#: anecdote, and this hint makes crops BIGGER, which can merge neighbours.
SEGMENT_MIN_SUPPORT = 2
#: Cap on how far a learned hint may grow a crop, as a fraction of its own
#: height/width.  A hint is a nudge towards the next ruling line, never a
#: licence to swallow the column.
SEGMENT_MAX_GROWTH = 0.9
#: Ink coverage that counts as "there is more of this notice here".
SEGMENT_INK_MIN = 0.02


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
        "crop_w": _crop_size(result)[0],
        "crop_h": _crop_size(result)[1],
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
    positive = sum(1 for r in records
                   if r["feedback"] in ("positive", "half_crop"))
    return positive, len(records) - positive


# --- learning -----------------------------------------------------------------

def _crop_size(result) -> Tuple[int, int]:
    """(width, height) of the crop the user judged, 0 when unknown."""
    image = getattr(result, "image_bgr", None)
    try:
        height, width = image.shape[:2]
        return int(width), int(height)
    except Exception:
        return 0, 0


def _method_base(method: str) -> str:
    """The detection path without its refinement suffixes: 'box+template?',
    'box+template+col', 'box+template+split' are all the same evidence about
    WHERE a detection came from."""
    return (method or "").replace("?", "").split("+col")[0].split("+split")[0]


def _region_bucket(source: str, method: str, width: int, height: int
                   ) -> Optional[str]:
    """Coarse layout class of one judged crop, or None when unknown.

    Deliberately coarse: newspaper + detection path + size class + shape
    class.  Fine enough that 'the small square photo ads Sandesh's page-scan
    keeps finding' is one bucket, coarse enough that a handful of clicks can
    ever fill one."""
    if not source or not width or not height:
        return None
    size = "s" if width < 400 else "m" if width < 750 else \
        "l" if width < 1200 else "xl"
    aspect = height / float(width)
    shape = "wide" if aspect < 0.6 else "tall" if aspect > 1.5 else "box"
    return f"{source}|{_method_base(method)}|{size}|{shape}"


def _record_bucket(entry: dict) -> Optional[str]:
    return _region_bucket(entry.get("source", ""),
                          entry.get("classifier_method", ""),
                          int(entry.get("crop_w") or 0),
                          int(entry.get("crop_h") or 0))


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
    # A half crop is a POSITIVE here.  The user said "this is the right
    # notice, badly cut" - training it as a negative would teach the
    # classifier to reject real notices, and recall is the one number that
    # may not drop.  The cropping half of that click is learned separately,
    # in the segmentation model.
    positive_docs = [r for r in records
                     if r["feedback"] in ("positive", "half_crop")]

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
    # The mirror image: what the user keeps CONFIRMING.  Same guard rails.
    # Used only to lift a Not Sure crop into the results (should_promote) -
    # never to create a detection, and never to outvote negative evidence.
    positive_weights: Dict[str, float] = {}
    for token, positives in pos_count.items():
        if positives < MIN_SUPPORT:
            continue
        negatives = neg_count.get(token, 0)
        if negatives and positives / float(negatives) < MIN_RATIO:
            continue
        positive_weights[token] = round(math.log1p(positives - negatives), 4)

    # Layout evidence: which (newspaper, path, size, shape) classes the user
    # keeps rejecting.  Text tokens miss the ads whose OCR is garbage - the
    # layout class is often the only stable thing about them.
    region_neg: Dict[str, int] = {}
    region_pos: Dict[str, int] = {}
    for bucket_counts, docs in ((region_neg, negative_docs),
                                (region_pos, positive_docs)):
        for entry in docs:
            bucket = _record_bucket(entry)
            if bucket:
                bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
    region_weights: Dict[str, float] = {}
    for bucket, negatives in region_neg.items():
        if negatives < REGION_MIN_SUPPORT:
            continue
        positives = region_pos.get(bucket, 0)
        if positives and negatives / float(positives) < MIN_RATIO:
            continue
        region_weights[bucket] = round(
            min(REGION_WEIGHT_CAP,
                REGION_WEIGHT_CAP * (negatives - positives) / 8.0), 4)

    return {
        "version": 1,
        "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "positive_examples": len(positive_docs),
        "negative_examples": len(negative_docs),
        "weights": weights,
        "positive_weights": positive_weights,
        "region_weights": region_weights,
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


def evaluate(model: dict, records: Sequence[dict]) -> dict:
    """How this model would have ruled on the user's own verdicts.  Pure.

    A demote of a confirmed-negative is the reward; a demote of a
    confirmed-positive is the injury.  `recall` is over confirmed positives:
    the fraction the model would have LEFT ALONE."""
    demoted_neg = demoted_pos = positives = negatives = 0
    promoted_neg = promoted_pos = 0
    for entry in records:
        text = entry.get("normalized_text", "")
        text_points = score(text, model)
        bucket = _record_bucket(entry)
        region_points = (model.get("region_weights") or {}).get(bucket, 0.0) \
            if bucket else 0.0
        confident = float(entry.get("classifier_score") or 0) \
            >= PROTECT_CONFIDENCE
        demoted = not confident and \
            (text_points + region_points) >= DEMOTE_SCORE
        promoted = entry.get("origin") == "review" and text_points == 0 \
            and score(text, model, "positive_weights") >= PROMOTE_SCORE
        if entry.get("feedback") in ("positive", "half_crop"):
            positives += 1
            demoted_pos += 1 if demoted else 0
            promoted_pos += 1 if promoted else 0
        else:
            negatives += 1
            demoted_neg += 1 if demoted else 0
            promoted_neg += 1 if promoted else 0
    recall = 1.0 if not positives else (positives - demoted_pos) / positives
    return {
        "positives": positives, "negatives": negatives,
        "caught_negatives": demoted_neg, "hurt_positives": demoted_pos,
        "promoted_positives": promoted_pos,
        "promoted_negatives": promoted_neg,
        "recall_on_confirmed": round(recall, 4),
        # A caught negative and a rightly promoted positive are the reward;
        # a hidden real notice costs three times as much - recall is the
        # contract - and a promoted piece of noise costs one.
        "reward": demoted_neg + promoted_pos - 3 * demoted_pos - promoted_neg,
    }


def _loo_injuries(records: Sequence[dict]) -> Tuple[int, int]:
    """Leave-one-out injury counts: (positives a model built WITHOUT them
    would demote, negatives such a model would promote).  Resubstitution
    can't answer that - a record's own tokens shield it while it is in the
    build set.  Records are few (user clicks), so the rebuilds are cheap."""
    held = [r for r in records if r.get("feedback") in ("positive",
                                                       "negative",
                                                       "half_crop")]
    if len(records) > 400:            # ponytail: cap, sample the newest
        held = held[-120:]
    hurt = promoted = 0
    for entry in held:
        rest = [r for r in records if r is not entry]
        out = evaluate(build_model(rest), [entry])
        hurt += out["hurt_positives"]
        promoted += out["promoted_negatives"]
    return hurt, promoted


def _log_history(entry: dict) -> None:
    entry = dict(entry, timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        with open(_path(LEARNING_HISTORY_FILENAME), "a",
                  encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def relearn() -> dict:
    """Rebuild from all evidence, validate, and save ONLY if the update
    cannot hurt a notice the user confirmed as real.

    The gate is the recall guard applied to learning itself: a candidate
    model that would demote any confirmed positive (tested leave-one-out,
    so a positive cannot protect itself) is HELD - the evidence stays in
    feedback.jsonl and the previous model keeps ruling.  Every decision is
    appended to learning_history.jsonl so improvement is measurable."""
    records = load_records()
    candidate = build_model(records)
    metrics = evaluate(candidate, records)
    has_rules = any(candidate.get(k) for k in
                    ("weights", "positive_weights", "region_weights"))
    hurt_loo, promoted_loo = _loo_injuries(records) if has_rules else (0, 0)
    metrics["hurt_positives_loo"] = hurt_loo
    metrics["promoted_negatives_loo"] = promoted_loo

    current = load_model()
    keep = hurt_loo == 0 and metrics["hurt_positives"] == 0 and \
        promoted_loo == 0 and metrics["promoted_negatives"] == 0
    _log_history({
        "candidate_rules": len(candidate.get("weights", {})),
        "candidate_positive_rules": len(candidate.get("positive_weights", {})),
        "candidate_region_rules": len(candidate.get("region_weights", {})),
        "metrics": metrics,
        "decision": "KEEP" if keep else "HOLD",
        "previous_version": current.get("version", 0),
    })
    if not keep:
        return current                # the click is stored; the model waits
    candidate["metrics"] = metrics
    return save_model(candidate)


# --- segmentation learning (Half Copy) ----------------------------------------

def _segment_bucket(source: str, method: str, width: int, height: int,
                    page_size: Sequence = ()) -> Optional[str]:
    """Layout class for a CROP, for segmentation learning.

    Deliberately not the image hash - "this exact picture was bad"
    generalises to nothing.  Newspaper + detection path + shape + which
    third of the page column it sits in is a pattern that recurs across
    editions, which is what makes "Sandesh box+ocr crops in this shape are
    usually cut short" learnable at all."""
    base = _region_bucket(source, method, width, height)
    if not base:
        return None
    try:
        page_w = int(page_size[0]) or 0
    except (TypeError, IndexError, ValueError):
        page_w = 0
    column = "?" if not page_w else str(int(3.0 * width / page_w))
    return f"{base}|col{column}"


def _edges_on_rules(gray, rect: Sequence) -> Dict[str, bool]:
    """Which of a rect's four edges sit on a printed ruling line.

    Uses the detector's own line masks, so "is there a border here?" is
    answered exactly the way segmentation answered it when it made the
    crop."""
    from .. import core
    import numpy as np

    x, y, w, h = (int(v) for v in rect)
    detector = core.BoxCandidateDetector(core.DETECTION_CONFIG)
    detector.compute_line_masks(gray)
    hmask, vmask = detector.horizontal_mask, detector.vertical_mask
    page_h, page_w = gray.shape[:2]
    tol = 5                       # a printed rule is a few pixels thick

    def horizontal(edge_y: int) -> bool:
        lo = max(0, edge_y - tol)
        hi = min(page_h, edge_y + tol + 1)
        seg = hmask[lo:hi, max(0, x):min(page_w, x + w)]
        if seg.size == 0:
            return True           # off the page: as good as a border
        return float(np.count_nonzero(seg.max(axis=0))) / max(1, w) >= 0.45

    def vertical(edge_x: int) -> bool:
        lo = max(0, edge_x - tol)
        hi = min(page_w, edge_x + tol + 1)
        seg = vmask[max(0, y):min(page_h, y + h), lo:hi]
        if seg.size == 0:
            return True
        return float(np.count_nonzero(seg.max(axis=1))) / max(1, h) >= 0.45

    return {"top": horizontal(y), "bottom": horizontal(y + h),
            "left": vertical(x), "right": vertical(x + w)}


def missing_direction(gray, rect: Sequence,
                      page_size: Sequence = ()) -> str:
    """Which way a crop was cut short: 'bottom', 'top', 'right', 'left',
    'multiple' or 'unknown'.

    Read off the PAGE, not guessed: for each edge, look at the band just
    outside the crop, the width (or height) of the crop itself, and ask
    whether printed ink continues there.  A notice that ends properly is
    followed by white space or a ruling line; one that was cut short is
    followed by more of its own text.

    `gray` is the page in the same (working) scale as `rect`."""
    try:
        import numpy as np
        x, y, w, h = (int(v) for v in rect)
    except (TypeError, ValueError):
        return "unknown"
    if gray is None or w <= 0 or h <= 0:
        return "unknown"
    page_h, page_w = gray.shape[:2]
    band = max(10, int(min(w, h) * 0.12))

    def ink(region) -> float:
        if region is None or region.size == 0:
            return 0.0
        return float(np.count_nonzero(region < 128)) / region.size

    edges = {
        "top": gray[max(0, y - band):max(0, y), x:min(page_w, x + w)],
        "bottom": gray[min(page_h, y + h):min(page_h, y + h + band),
                       x:min(page_w, x + w)],
        "left": gray[y:min(page_h, y + h), max(0, x - band):max(0, x)],
        "right": gray[y:min(page_h, y + h),
                      min(page_w, x + w):min(page_w, x + w + band)],
    }
    # "Is there ink just outside?" is NOT the question - on a notice-board
    # page there is always ink just outside, because the next notice is
    # there.  Measured on 30 real Sandesh crops, that test alone called 28
    # of 30 COMPLETE crops short.
    #
    # The question is whether this crop ENDS somewhere.  A complete notice
    # stops at its own printed border; a crop cut short stops in the middle
    # of the text with no rule under it.  So an edge sitting on a ruling
    # line is complete whatever lies beyond it, and only an edge with no
    # rule AND text carrying on past it counts as short.
    ruled = _edges_on_rules(gray, (x, y, w, h))
    inside = ink(gray[y:y + h, x:x + w])
    floor = max(SEGMENT_INK_MIN, inside * 0.35)
    hits = [name for name, region in edges.items()
            if not ruled.get(name) and ink(region) >= floor]
    if not hits:
        return "unknown"
    if len(hits) > 1:
        # Bottom wins when it is one of them: Gujarati notices run down the
        # column, so a crop that is short is nearly always short at the
        # bottom, and expanding one way is safer than expanding two.
        return "bottom" if "bottom" in hits else "multiple"
    return hits[0]


def load_segmentation() -> dict:
    try:
        with open(_path(SEGMENT_FILENAME), encoding="utf-8") as handle:
            model = json.load(handle)
    except (OSError, ValueError):
        model = {}
    if not isinstance(model, dict):
        model = {}
    model.setdefault("version", 0)
    model.setdefault("buckets", {})
    model.setdefault("half_crops", 0)
    model.setdefault("confirmed", 0)
    return model


def _save_segmentation(model: dict) -> dict:
    path = _path(SEGMENT_FILENAME)
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


def record_half_crop(result, direction: str = "unknown",
                     page_size: Sequence = ()) -> dict:
    """One Half Copy click: the crop was incomplete, the notice was right.

    Writes to the SAME evidence file as every other click (one history, one
    place to audit) with feedback "half_crop", and updates the segmentation
    model.  The classifier reads it as a POSITIVE - see the note at the top
    of this section."""
    from . import search as search_util
    from .. import core

    width, height = _crop_size(result)
    entry = record(result, "half_crop", origin="results")
    entry.update({
        "direction": direction,
        "crop": {"x": int(getattr(result, "page_rect", (0, 0, 0, 0))[0]),
                 "y": int(getattr(result, "page_rect", (0, 0, 0, 0))[1]),
                 "width": int(getattr(result, "page_rect", (0, 0, 0, 0))[2]),
                 "height": int(getattr(result, "page_rect", (0, 0, 0, 0))[3])},
        "crop_pixels": {"width": width, "height": height},
        "page_dimensions": {"width": int(page_size[0]) if page_size else 0,
                            "height": int(page_size[1]) if page_size else 0},
        "segmentation_version": load_segmentation().get("version", 0),
    })
    # Rewrite the last line with the richer record: record() already wrote a
    # plain one, and the evidence file must hold ONE row per click.
    _replace_last_record(entry)

    model = load_segmentation()
    bucket = _segment_bucket(getattr(result, "newspaper", ""),
                             getattr(result, "method", ""), width, height,
                             page_size)
    if bucket:
        stats = model["buckets"].setdefault(
            bucket, {"directions": {}, "half": 0, "ok": 0})
        stats["directions"][direction] = \
            stats["directions"].get(direction, 0) + 1
        stats["half"] = stats.get("half", 0) + 1
    model["half_crops"] = int(model.get("half_crops", 0)) + 1
    model["version"] = int(model.get("version", 0)) + 1
    _save_segmentation(model)
    return entry


def record_crop_confirmed(result) -> None:
    """A crop the user confirmed as right (This Is Right in Not Sure).

    The positive half of the segmentation reward: a bucket that keeps being
    confirmed must not drift into "always expand" because of two old half
    crops."""
    model = load_segmentation()
    bucket = _segment_bucket(getattr(result, "newspaper", ""),
                             getattr(result, "method", ""),
                             *_crop_size(result),
                             getattr(result, "page_size", ()))
    if bucket:
        stats = model["buckets"].setdefault(
            bucket, {"directions": {}, "half": 0, "ok": 0})
        stats["ok"] = stats.get("ok", 0) + 1
    model["confirmed"] = int(model.get("confirmed", 0)) + 1
    _save_segmentation(model)


def _replace_last_record(entry: dict) -> None:
    """Swap the final line of the evidence file for `entry` (same id)."""
    path = _path(FEEDBACK_FILENAME)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return
        lines[-1] = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
    except OSError:
        pass


def expansion_hint(newspaper: str, method: str, width: int, height: int,
                   page_size: Sequence = ()) -> str:
    """Which way crops of this layout class have been reported short.

    Returns a direction, or "" when there is not enough agreeing evidence.
    Two guards, both about not making things worse: the bucket needs
    SEGMENT_MIN_SUPPORT reports of the SAME direction, and a bucket whose
    crops have been confirmed correct at least as often as reported short
    gets no hint at all."""
    model = load_segmentation()
    bucket = _segment_bucket(newspaper, method, width, height, page_size)
    stats = (model.get("buckets") or {}).get(bucket or "")
    if not stats:
        return ""
    if int(stats.get("ok", 0)) >= int(stats.get("half", 0)):
        return ""
    directions = stats.get("directions") or {}
    if not directions:
        return ""
    best = max(directions, key=lambda key: directions[key])
    if best in ("unknown", "multiple"):
        return ""
    return best if directions[best] >= SEGMENT_MIN_SUPPORT else ""


def half_crop_rate() -> Tuple[int, int, float]:
    """(half crops, notices judged, rate) - what §29 asks to be tracked."""
    records = load_records()
    half = sum(1 for r in records if r.get("feedback") == "half_crop")
    total = len(records)
    return half, total, (half / total if total else 0.0)


# --- applying it --------------------------------------------------------------

def score(normalized_text: str, model: Optional[dict] = None,
          key: str = "weights") -> float:
    """How much learned evidence this text carries - NEGATIVE by default,
    positive with key="positive_weights".  0.0 = none."""
    model = load_model() if model is None else model
    weights = model.get(key) or {}
    if not weights or not normalized_text:
        return 0.0
    total = 0.0
    for token in _tokens(normalized_text):
        total += weights.get(token, 0.0)
    return round(total, 4)


#: Positive evidence a Not Sure crop needs before it skips the queue.
PROMOTE_SCORE = 2.0


def should_promote(result, model: Optional[dict] = None) -> bool:
    """Has this Not Sure crop earned its way INTO the results?

    Only crops already in the review queue, only when the learned positive
    evidence is strong and there is NO learned negative evidence at all.
    Promotion never creates a detection - it moves one the detector already
    made from one list to the other, and a wrong promotion is one Not
    Related click away (which then teaches the opposite)."""
    if not getattr(result, "needs_review", False) or \
            not getattr(result, "ocr_done", False):
        return False
    model = load_model() if model is None else model
    text = getattr(result, "normalized_ocr", "") or ""
    if score(text, model) > 0:
        return False
    return score(text, model, "positive_weights") >= PROMOTE_SCORE


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
    points = score(getattr(result, "normalized_ocr", "") or "", model)
    width, height = _crop_size(result)
    bucket = _region_bucket(getattr(result, "newspaper", ""),
                            getattr(result, "method", ""), width, height)
    if bucket:
        points += (model.get("region_weights") or {}).get(bucket, 0.0)
    return points >= DEMOTE_SCORE


# --- self-check ---------------------------------------------------------------

class _Result:
    def __init__(self, text, confidence=70, done=True, size=None,
                 newspaper="Gujarat Samachar", method="box+template"):
        from .. import core
        self.ocr_text = text
        self.normalized_ocr = core.normalize_ocr_text(text)
        self.confidence = confidence
        self.ocr_done = done
        self.result_id = 1
        self.newspaper = newspaper
        self.page_number = 4
        self.edition = "ahmedabad"
        self.issue_date = "2026-08-12"
        self.notice_type = "notice"
        self.method = method
        self.image_bgr = None
        if size is not None:
            import numpy as np
            self.image_bgr = np.zeros((size[1], size[0], 3), dtype="uint8")


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

    # Region learning: repeated rejections of one layout class (different,
    # garbage text each time - the OCR-noise ad case) become bucket evidence.
    for index in range(REGION_MIN_SUPPORT):
        record(_Result(f"zxq{index} qqwx{index} vv{index}", size=(300, 300),
                       newspaper="Sandesh", method="page-scan"), "negative")
    model = build_model()
    assert model["region_weights"], "layout rejections taught nothing"
    # ...but layout alone can never demote (its cap < DEMOTE_SCORE).
    fresh_ad = _Result("totally new words here", size=(310, 290),
                       newspaper="Sandesh", method="page-scan")
    save_model(model)
    assert not should_demote(fresh_ad, load_model()), \
        "layout evidence alone demoted a crop"

    # The relearn gate: a model that would demote a confirmed POSITIVE is
    # held, not saved.  Five rejections and one confirmation of the same
    # text - leave-one-out shows the model would hurt that positive.
    trap = "mango festival gift city special stalls booking open today"
    for _ in range(5):
        record(_Result(trap), "negative")
    record(_Result(trap), "positive")
    before = load_model().get("version", 0)
    ruled = relearn()
    history_path = _path(LEARNING_HISTORY_FILENAME)
    assert os.path.exists(history_path), "no learning history written"
    last = json.loads(open(history_path,
                           encoding="utf-8").readlines()[-1])
    assert last["decision"] == "HOLD", last
    assert ruled.get("version", 0) == before, "a harmful model was saved"

    # Positive learning: repeated confirmations of a KIND lift a similar
    # Not Sure crop into the results - and only a Not Sure crop.
    for f in (path, learned, history_path):
        if os.path.exists(f):
            os.remove(f)
    court = ("public notice in the court of the civil judge senior division "
             "notice to defendant suit number")
    for _ in range(MIN_SUPPORT + 1):
        record(_Result(court + " ahmedabad"), "positive", "review")
    model = build_model()
    assert model["positive_weights"], "repeated confirmations taught nothing"
    unsure = _Result("public notice in the court of the civil judge "
                     "senior division notice to defendant gandhinagar",
                     confidence=64)
    unsure.needs_review = True
    assert should_promote(unsure, model), "a confirmed kind stayed in review"
    settled = _Result(court, confidence=64)          # not in the queue
    settled.needs_review = False
    assert not should_promote(settled, model)
    # ...and never against negative evidence: the same words rejected too.
    for _ in range(MIN_SUPPORT + 1):
        record(_Result(court + " ahmedabad"), "negative", "review")
    model = build_model()
    assert not model["positive_weights"], "contested words became a rule"

    for f in (path, learned, history_path):
        if os.path.exists(f):
            os.remove(f)
    print("feedback self-check OK")


if __name__ == "__main__":
    demo()
