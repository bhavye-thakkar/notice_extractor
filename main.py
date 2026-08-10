#!/usr/bin/env python3
"""Public Notice Extractor - entry point.

    python notice_extractor/main.py                 the desktop app
    python -m notice_extractor.main --headless      one run, no window
    python -m notice_extractor.main --headless --paper "Divya Bhaskar"
    python -m notice_extractor.main --headless --date 2026-08-08 --pages 4

The headless mode exists so a run can be scripted, scheduled, or benchmarked -
and so the log of a real run can be read afterwards without watching a window.
It uses the same agents/processor.py pipeline as the buttons do.
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time

# Running from another working directory (a shortcut, a scheduled task) must
# still find the notice_extractor package.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Bytecode caches go to the machine cache, not the source tree.  Spelled out
# rather than read from config.py because importing config is itself an
# import, and the prefix has to be set before one happens (same reason the
# launcher repeats it).  Reaching here via `python -m` still leaves one
# __pycache__ for this module; closing the app sweeps it.
sys.pycache_prefix = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "PublicNoticeExtractor", "pycache") if sys.platform.startswith("win") \
    else os.path.join(os.path.expanduser("~"), ".cache",
                      "public-notice-extractor", "pycache")

from notice_extractor import config                       # noqa: E402
from notice_extractor.utils import logger as run_logger   # noqa: E402


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="notice_extractor",
        description="Extract Public Notices (જાહેર નોટિસ) from Gujarati "
                    "e-papers.")
    parser.add_argument("--headless", action="store_true",
                        help="run one extraction without opening the window")
    parser.add_argument("--paper", default="",
                        help="newspaper display name (default: all online "
                             "papers)")
    parser.add_argument("--edition", default="",
                        help="edition slug, e.g. ahmedabad")
    parser.add_argument("--date", default="",
                        help="ISO date; default is config.TARGET_DATE "
                             "('auto' = today)")
    parser.add_argument("--pages", type=int, default=0,
                        help="stop after N pages per edition (a quick check)")
    parser.add_argument("--save", default="",
                        help="folder to write the cropped notices into")
    return parser.parse_args(argv)


def _headless(args) -> int:
    """One run on the console: same agents, same logs, no Tk."""
    from notice_extractor import core, scrapers
    from notice_extractor.agents.processor import run_jobs

    # A scripted run must never stop to wait for a sign-in window nobody is
    # watching; it uses whatever session this machine already has.
    config.BROWSER_ALLOW_INTERACTIVE_LOGIN = False

    missing = core.missing_dependencies()
    if missing:
        print("Missing dependencies: " + ", ".join(missing))
        print("Install them with:  pip install " + " ".join(missing))
        return 2

    core.register_newspapers(scrapers.load_all())
    if not core.NEWSPAPER_REGISTRY:
        print("No newspaper scrapers could be loaded.")
        return 2

    day = config.resolve_target_date(args.date)
    papers = []
    for cls in core.NEWSPAPER_REGISTRY.values():
        if cls is core.local_pdf_extractor():
            continue
        if args.paper and cls.display_name.lower() != args.paper.lower():
            continue
        papers.append(cls)
    if not papers:
        print(f"No newspaper matches --paper {args.paper!r}.  Known: "
              + ", ".join(core.NEWSPAPER_REGISTRY))
        return 2
    if args.pages:
        core.PAGE_LIMIT[0] = args.pages

    jobs = []
    for cls in papers:
        editions = ((args.edition,) if args.edition
                    else cls.get_loop_editions())
        for edition in editions:
            jobs.append((cls, edition, day, cls.build_url(edition, day)))

    run_logger.banner(f"{core.APP_TITLE} headless: {len(jobs)} edition(s) "
                      f"for {day.isoformat()}")
    print(f"{core.APP_TITLE} - {len(jobs)} edition(s) for {day.isoformat()}")

    msg_queue: "queue.Queue" = queue.Queue()
    reporter = core.ProgressReporter(msg_queue, threading.Event())
    results = []

    def drain() -> None:
        while True:
            try:
                message = msg_queue.get_nowait()
            except queue.Empty:
                return
            kind = message[0]
            if kind == "log":
                level = message[2]
                marker = {"error": "!!", "warn": " !", "success": " +"}.get(
                    level, "  ")
                print(f"{marker} {message[1]}")
                run_logger.log(message[1], level)
            elif kind == "result":
                results.append(message[1])
            elif kind == "heading":
                print(f"\n== {message[1]}")

    outcome = {}

    def run() -> None:
        outcome["summary"] = run_jobs(jobs, reporter)

    worker = threading.Thread(target=run, daemon=True, name="headless-run")
    worker.start()
    while worker.is_alive():
        drain()
        time.sleep(0.2)
    drain()

    summary = outcome.get("summary")
    print(f"\n{len(results)} notice(s) found.")
    if args.save and results:
        folder = os.path.abspath(args.save)
        if config.is_inside_data(folder):
            print("  warning: that folder is inside notice_extractor/data, "
                  "which the app clears when it closes - saving elsewhere is "
                  "safer.")
        os.makedirs(folder, exist_ok=True)
        for result in results:
            core.save_image_unicode(
                result.image_bgr,
                os.path.join(folder, result.suggested_filename))
        print(f"Saved {len(results)} crop(s) to {folder}")
    print(f"Log: {run_logger.log_path()}")
    run_logger.close()
    # Finding nothing is a valid answer (some days have no notices); only a
    # failed or cancelled run is an error, so a scheduler can trust this.
    if summary is None or summary.cancelled:
        return 1
    return 1 if summary.skipped and not summary.per_paper else 0


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    run_logger.prune()
    if args.headless:
        return _headless(args)
    from notice_extractor.ui.app import run
    return run()


if __name__ == "__main__":
    sys.exit(main())
