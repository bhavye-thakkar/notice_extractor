#!/usr/bin/env python3
"""Score a validate_pages.py report against a ground-truth file, and diff it
against the previous run - the numbers behind every KEEP / REVERT decision.

    python -m notice_extractor.tools.measure_validation <validation.json> \
        <ground_truth.json> [previous_validation.json] [--label NAME]

Ground truth (tools/validation/*.json) lists the expected notice rects per
page in working-scale coordinates.  A detection matches an expected notice
when their IoU >= 0.5.  Detections flagged `review` are neither TP nor FP
(they are what the Not Sure queue is for) but they are counted, because a
tender parked there is still noise the user must click through.

Prints the iteration report and appends one line to
data/validation_history.jsonl so improvement (or regression) is on record.
"""

from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = HERE
while ROOT != os.path.dirname(ROOT):
    if os.path.isdir(os.path.join(ROOT, "notice_extractor")):
        break
    ROOT = os.path.dirname(ROOT)
sys.path.insert(0, ROOT)


def iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / float(union) if union else 0.0


def _containment(inner, outer) -> float:
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    x = max(0, min(ix + iw, ox + ow) - max(ix, ox))
    y = max(0, min(iy + ih, oy + oh) - max(iy, oy))
    return (x * y) / float(iw * ih) if iw * ih else 0.0


def score(report: dict, truth: dict) -> dict:
    """Match detections to expected rects page by page (greedy by IoU)."""
    expected = {int(k): [tuple(r) for r in v]
                for k, v in truth["expected"].items()}
    tp = fp = fn = review = fragments = 0
    misses, extras = [], []
    pages = {p["page"]: p["detections"] for p in report["pages"]}
    for page in sorted(set(expected) | set(pages)):
        want = list(expected.get(page, []))
        got = list(pages.get(page, []))
        review += sum(1 for d in got if d.get("review"))
        results = [d for d in got if not d.get("review")]
        matched = set()
        for d in results:
            best, best_i = 0.0, -1
            for i, w in enumerate(want):
                if i in matched:
                    continue
                v = iou(d["rect"], w)
                if v > best:
                    best, best_i = v, i
            if best >= 0.5:
                tp += 1
                matched.add(best_i)
            else:
                # A crop that lies INSIDE an expected notice is a fragment,
                # not an unrelated extra - reported apart, because the two
                # need different fixes.
                inside = any(_containment(d["rect"], w) >= 0.8 for w in want)
                fragments += 1 if inside else 0
                fp += 0 if inside else 1
                extras.append((page, d["rect"], d.get("method"),
                               "fragment" if inside else "extra"))
        for i, w in enumerate(want):
            if i not in matched:
                fn += 1
                misses.append((page, w))
    precision = tp / float(tp + fp) if tp + fp else 1.0
    recall = tp / float(tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) \
        if precision + recall else 0.0
    return {"expected": tp + fn, "detected": tp + fp + fragments,
            "tp": tp, "fp": fp, "fn": fn, "fragments": fragments,
            "review": review, "precision": round(precision, 4),
            "recall": round(recall, 4), "f1": round(f1, 4),
            "misses": misses, "extras": extras}


def report_text(current: dict, previous: dict = None, label: str = "") -> str:
    def prev(key):
        return previous[key] if previous else "-"
    lines = [
        "=" * 40, "SANDESH ITERATION REPORT", "=" * 40,
        f"Run:       {label or time.strftime('%Y-%m-%d %H:%M')}",
        f"Previous:  {previous.get('label', 'yes') if previous else 'none'}",
        "",
        f"Notices Expected:  {current['expected']}",
        f"Notices Detected:  {current['detected']}    (prev {prev('detected')})",
        "",
        f"True Positives:    {current['tp']}    (prev {prev('tp')})",
        f"False Positives:   {current['fp']}    (prev {prev('fp')})",
        f"False Negatives:   {current['fn']}    (prev {prev('fn')})",
        f"Fragments:         {current['fragments']}    (prev {prev('fragments')})",
        f"In Review queue:   {current['review']}    (prev {prev('review')})",
        "",
        f"Precision:  {current['precision']:.4f}   (prev {prev('precision')})",
        f"Recall:     {current['recall']:.4f}   (prev {prev('recall')})",
        f"F1:         {current['f1']:.4f}   (prev {prev('f1')})",
    ]
    decision = "KEEP"
    if previous:
        if current["recall"] < previous["recall"] or \
                current["fn"] > previous["fn"]:
            decision = "REVERT (recall regressed)"
        elif current["fp"] > previous["fp"] + 2:
            decision = "REVERT (false positives up)"
    if current["recall"] < 0.98:
        decision += "  [recall below 98% target]"
    lines += ["", f"Decision:  {decision}", "=" * 40]
    if current["misses"]:
        lines.append("Missed:")
        lines += [f"  p{p:02d} {r}" for p, r in current["misses"]]
    if current["extras"]:
        lines.append("Extras / fragments:")
        lines += [f"  p{p:02d} {r} {m} [{k}]" for p, r, m, k in current["extras"]]
    return "\n".join(lines)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    label = ""
    if "--label" in sys.argv:
        label = sys.argv[sys.argv.index("--label") + 1]
        args = [a for a in args if a != label]
    if len(args) < 2:
        print(__doc__)
        return 2
    report = json.load(open(args[0], encoding="utf-8"))
    report = report[0] if isinstance(report, list) else report
    truth = json.load(open(args[1], encoding="utf-8"))
    current = score(report, truth)
    previous = None
    if len(args) > 2 and os.path.exists(args[2]):
        prev_report = json.load(open(args[2], encoding="utf-8"))
        prev_report = prev_report[0] if isinstance(prev_report, list) \
            else prev_report
        previous = score(prev_report, truth)
        previous["label"] = os.path.basename(args[2])
    print(report_text(current, previous, label))
    from notice_extractor import config
    entry = {k: v for k, v in current.items() if k not in ("misses", "extras")}
    entry.update({"label": label, "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "truth": os.path.basename(args[1])})
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(os.path.join(config.DATA_DIR, "validation_history.jsonl"),
                  "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
