#!/usr/bin/env python3
"""Before/after benchmark for the Public Notice Extractor pipeline.

Runs BOTH scheduler configurations inside ONE process, alternating between
them, and reports the median of several trials.  That matters: this workload
is dominated by cv2.matchTemplate and single measurements on a busy desktop
vary by more than 2x, so comparing two separate script runs is meaningless.

    python benchmark_pipeline.py [--network-ms 1200] [--trials 3]

The two configurations mirror the real code exactly:

  BEFORE  4 edition agents, cv2 threads = cpu // agents, no detect gate
          (the old MAX_PARALLEL_JOBS = 4 plus the old setNumThreads division)
  AFTER   all editions as agents at once, cv2 threads = CV2_THREADS_PER_DETECT,
          CPU-heavy detect() bounded by the shared _detect_gate

No network and no newspaper site is touched; pages are synthesised and the
download is modelled as a sleep, because what the change actually moves is how
much downloading overlaps a bounded amount of detection.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import os
import statistics
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# The folder that CONTAINS notice_extractor/ - found by walking up rather
# than assuming a depth, because tools/ has lived both at the project root
# and inside the package, and "../.." silently breaks when it moves.
ROOT = HERE
while ROOT != os.path.dirname(ROOT):
    if os.path.isdir(os.path.join(ROOT, "notice_extractor")):
        break
    ROOT = os.path.dirname(ROOT)
sys.path.insert(0, ROOT)
TARGET = os.path.join(ROOT, "notice_extractor", "core.py")

EDITIONS = 12          # a realistic "All Newspapers" run
PAGES_PER_EDITION = 4  # kept small so a trial can be repeated
USE_OCR = True         # --no-ocr isolates the scheduler from OCR cost


def load_module():
    from notice_extractor import core
    return core


def synthetic_page(pne, width=1600, height=2200):
    """A newspaper-ish page: column rules, bordered ad boxes, body-text ink."""
    np = pne.np
    cv2 = pne.cv2
    rng = np.random.default_rng(7)
    page = np.full((height, width, 3), 245, dtype=np.uint8)

    for y in range(60, height - 60, 14):
        for x0 in range(40, width - 40, 190):
            run = int(rng.integers(60, 170))
            cv2.line(page, (x0, y), (min(x0 + run, width - 40), y),
                     (40, 40, 40), 2)

    for bx, by, bw, bh in ((120, 300, 420, 520), (700, 260, 480, 700),
                           (1200, 1100, 330, 480), (200, 1300, 500, 600)):
        cv2.rectangle(page, (bx, by), (bx + bw, by + bh), (0, 0, 0), 3)
        for y in range(by + 40, by + bh - 20, 16):
            cv2.line(page, (bx + 18, y), (bx + bw - 18, y), (30, 30, 30), 2)
    return page


class Config:
    def __init__(self, name, agents, cv2_threads, detect_gate):
        self.name = name
        self.agents = agents
        self.cv2_threads = cv2_threads
        self.detect_gate = detect_gate


def run_trial(pne, page, cfg, network_ms):
    """N editions x P pages under `cfg`, mimicking the app's real shape:
    one pipeline per edition, then per page download-then-detect."""
    reporter = pne._SilentReporter()
    pne.cv2.setNumThreads(cfg.cv2_threads)
    # Swap in a gate of the right width (a very wide one == the old no-gate).
    original_gate = pne._detect_gate
    pne._detect_gate = threading.BoundedSemaphore(cfg.detect_gate)

    peak = {"n": 0, "cur": 0}
    lock = threading.Lock()

    def one_edition(_index):
        with lock:
            peak["cur"] += 1
            peak["n"] = max(peak["n"], peak["cur"])
        try:
            engine = pne.select_ocr_engine(reporter) if USE_OCR else None
            pipeline = pne.NoticeDetectionPipeline(reporter=reporter,
                                                   ocr_engine=engine)
            for _page in range(PAGES_PER_EDITION):
                time.sleep(network_ms / 1000.0)     # download
                pipeline.detect(page)               # detect (gated)
        finally:
            with lock:
                peak["cur"] -= 1

    try:
        t = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=cfg.agents) as pool:
            list(pool.map(one_edition, range(EDITIONS)))
        elapsed = time.perf_counter() - t
    finally:
        pne._detect_gate = original_gate
    return elapsed, peak["n"]


def main() -> int:
    global EDITIONS, PAGES_PER_EDITION, USE_OCR
    ap = argparse.ArgumentParser()
    ap.add_argument("--network-ms", type=int, default=1200,
                    help="modelled per-page download time (real e-paper "
                         "page JPEGs are 1-5 MB)")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--editions", type=int, default=EDITIONS)
    ap.add_argument("--pages", type=int, default=PAGES_PER_EDITION)
    ap.add_argument("--no-ocr", action="store_true",
                    help="detect with template matching only, isolating the "
                         "scheduler change from OCR cost")
    args = ap.parse_args()

    EDITIONS = args.editions
    PAGES_PER_EDITION = args.pages
    USE_OCR = not args.no_ocr
    # Progress must be visible while a long run is in flight, not only at exit.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    print(f"loading {TARGET} ...")
    pne = load_module()
    cpus = os.cpu_count() or 4

    before = Config("BEFORE (4 agents, no gate)",
                    agents=min(4, EDITIONS),
                    cv2_threads=max(1, cpus // min(4, EDITIONS)),
                    detect_gate=999)
    after = Config(f"AFTER  ({min(pne.MAX_PARALLEL_JOBS, EDITIONS)} agents, "
                   f"gate={pne.DETECT_CONCURRENCY})",
                   agents=pne.resolve_job_workers(EDITIONS),
                   cv2_threads=pne.CV2_THREADS_PER_DETECT,
                   detect_gate=pne.DETECT_CONCURRENCY)

    engine = pne.select_ocr_engine(pne._SilentReporter()) if USE_OCR else None
    print(f"\ncpus={cpus}  ocr={engine.name if engine else 'OFF'}  "
          f"gujarati={bool(engine and engine.supports_gujarati)}")
    print(f"workload: {EDITIONS} editions x {PAGES_PER_EDITION} pages, "
          f"download modelled at {args.network_ms} ms/page")
    for cfg in (before, after):
        print(f"  {cfg.name:38} agents={cfg.agents:2}  "
              f"cv2_threads={cfg.cv2_threads}  gate={cfg.detect_gate}")

    page = synthetic_page(pne)
    # Warm every cache (templates, fonts, engine) so trial 1 is not penalised.
    pne.NoticeDetectionPipeline(reporter=pne._SilentReporter(),
                                ocr_engine=engine).detect(page)

    results = {before.name: [], after.name: []}
    peaks = {}
    print(f"\nrunning {args.trials} alternating trials per config...")
    for trial in range(1, args.trials + 1):
        # Alternate the order: running one config always second would hand it
        # every warm-cache and CPU-boost advantage.
        order = (before, after) if trial % 2 else (after, before)
        for cfg in order:
            elapsed, peak = run_trial(pne, page, cfg, args.network_ms)
            results[cfg.name].append(elapsed)
            peaks[cfg.name] = peak
            print(f"  trial {trial}  {cfg.name:38} {elapsed:7.2f}s  "
                  f"(peak agents {peak})")

    print("\n" + "=" * 72)
    med_before = statistics.median(results[before.name])
    med_after = statistics.median(results[after.name])
    for cfg, med in ((before, med_before), (after, med_after)):
        samples = "  ".join(f"{s:.2f}" for s in results[cfg.name])
        print(f"{cfg.name:38} median {med:7.2f}s   [{samples}]  "
              f"peak agents {peaks[cfg.name]}")
    print("-" * 72)
    delta = (med_before - med_after) / med_before * 100.0
    print(f"{'speedup':38} {med_before / med_after:7.2f}x  "
          f"({delta:+.1f}% wall clock)")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
