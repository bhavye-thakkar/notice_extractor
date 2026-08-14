#!/usr/bin/env python3
"""Replay detection over a folder of saved newspaper pages and report every
detection - the segmentation regression harness.

    python -m notice_extractor.tools.validate_pages <pages-folder>

<pages-folder> holds one sub-folder per newspaper (folder name = display
name with spaces as underscores, e.g. Gujarat_Samachar) containing
pXX_page.png full-resolution pages - the capture a real run saves (see
flow.md "Validating segmentation changes").  For each page this prints the
final detections (rect / method / score / review flag) and writes
pXX_overlay.png next to it, so a segmentation change can be judged against
the same physical pages before and after.

Pages are deliberately NOT in the repository: they are that day's
newspapers (large, copyrighted, and refreshable any day by re-capturing).
The harness is what is kept.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = HERE
while ROOT != os.path.dirname(ROOT):
    if os.path.isdir(os.path.join(ROOT, "notice_extractor")):
        break
    ROOT = os.path.dirname(ROOT)
sys.path.insert(0, ROOT)

from notice_extractor import core, scrapers            # noqa: E402


def validate_folder(folder: str) -> dict:
    cv2 = core.cv2
    name = os.path.basename(folder).replace("_", " ")
    cls = core.NEWSPAPER_REGISTRY.get(name)
    reporter = core._SilentReporter()
    engine = core.select_ocr_engine(reporter)
    pipeline_cls = cls.pipeline_cls if cls else core.NoticeDetectionPipeline
    report = {"paper": name, "pages": []}
    for entry in sorted(os.listdir(folder)):
        if not entry.endswith("_page.png"):
            continue
        page_number = int(entry[1:3])
        image = cv2.imread(os.path.join(folder, entry), cv2.IMREAD_COLOR)
        if image is None:
            continue
        pipe = pipeline_cls(reporter=reporter, ocr_engine=engine)
        detections = pipe.detect(image)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if pipe._scale != 1.0:
            gray = cv2.resize(gray, (int(gray.shape[1] * pipe._scale),
                                     int(gray.shape[0] * pipe._scale)),
                              interpolation=cv2.INTER_AREA)
        overlay = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        rows = []
        for index, det in enumerate(detections, 1):
            x, y, w, h = det.rect
            cv2.rectangle(overlay, (x, y), (x + w, y + h), (0, 200, 0), 4)
            cv2.putText(overlay, f"{index}:{det.method}:{det.score:.2f}",
                        (x + 4, max(24, y - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 140, 0), 2)
            rows.append({"rect": [int(v) for v in det.rect],
                         "method": det.method,
                         "score": round(float(det.score), 3),
                         "family": det.family,
                         "review": bool(det.uncertain)})
            print(f"  p{page_number:02d} #{index} {det.rect} "
                  f"{det.method} {det.score:.2f}"
                  f"{'  REVIEW' if det.uncertain else ''}")
        width = 1100
        height = int(overlay.shape[0] * width / overlay.shape[1])
        cv2.imwrite(os.path.join(folder, f"p{page_number:02d}_overlay.png"),
                    cv2.resize(overlay, (width, height),
                               interpolation=cv2.INTER_AREA))
        report["pages"].append({"page": page_number, "detections": rows})
    return report


def main() -> int:
    if len(sys.argv) < 2 or not os.path.isdir(sys.argv[1]):
        print(__doc__)
        return 2
    core.register_newspapers(scrapers.load_all())
    base = sys.argv[1]
    folders = [os.path.join(base, d) for d in sorted(os.listdir(base))
               if os.path.isdir(os.path.join(base, d))]
    if not folders:
        folders = [base]
    full = []
    for folder in folders:
        print(f"== {os.path.basename(folder)}")
        full.append(validate_folder(folder))
    out = os.path.join(base, "validation.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(full, handle, ensure_ascii=False, indent=1)
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
