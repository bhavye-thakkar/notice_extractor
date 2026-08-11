#!/usr/bin/env python3
"""Live QA: drive the REAL application, measure it, report.

This is not a source review.  It builds the actual Tk window, walks its
widget tree, invokes the real buttons, fires real scroll events, runs a real
extraction and measures how long the UI thread is blocked while it happens.

    python tools/qa_run.py                 quick pass (2 pages per edition)
    python tools/qa_run.py --pages 4       deeper
    python tools/qa_run.py --paper Sandesh one newspaper

Results append to data/performance/runs.json so each run can be compared
with the one before it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox
from typing import Dict, List, Optional, Tuple

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

from notice_extractor import config, core, scrapers          # noqa: E402
from notice_extractor.utils import search as search_util     # noqa: E402

PERF_DIR = os.path.join(config.DATA_DIR, "performance")
RUNS_FILE = os.path.join(PERF_DIR, "runs.json")

#: Buttons that would end the session or open a native dialog we cannot
#: drive.  They are checked for existence and state, never invoked.
DO_NOT_CLICK = ("Exit", "Download Dependencies", "Open PDF...")


# =============================================================================
# report plumbing
# =============================================================================
class Report:
    def __init__(self) -> None:
        self.checks: List[Tuple[str, str, bool, str]] = []
        self.metrics: Dict[str, object] = {}

    def check(self, section: str, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((section, name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f"  - {detail}" if detail else ""),
              flush=True)
        return ok

    @property
    def failures(self) -> List[Tuple[str, str, bool, str]]:
        return [c for c in self.checks if not c[2]]

    def section_status(self, section: str) -> str:
        rows = [c for c in self.checks if c[0] == section]
        if not rows:
            return "n/a"
        return "PASS" if all(r[2] for r in rows) else "FAIL"


class Heartbeat:
    """A 10 ms tick on the Tk loop.  The worst gap between ticks IS the click
    latency: a button press waits in the same queue."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.gaps: List[float] = []
        self._last = time.perf_counter()
        self._stop = False
        self._schedule()

    def _schedule(self) -> None:
        if not self._stop:
            self.root.after(10, self._tick)

    def _tick(self) -> None:
        now = time.perf_counter()
        self.gaps.append(now - self._last)
        self._last = now
        self._schedule()

    def reset(self) -> None:
        self.gaps.clear()
        self._last = time.perf_counter()

    def worst_ms(self) -> float:
        return max(self.gaps) * 1000 if self.gaps else 0.0

    def median_ms(self) -> float:
        if not self.gaps:
            return 0.0
        ordered = sorted(self.gaps)
        return ordered[len(ordered) // 2] * 1000

    def stop(self) -> None:
        self._stop = True


def pump(root: tk.Tk, seconds: float) -> None:
    """Run the real event loop for a while."""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        root.update()
        time.sleep(0.005)


def pump_until(root: tk.Tk, predicate, timeout: float) -> bool:
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        root.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def quiesce(root: tk.Tk, app, report: "Report", why: str,
            timeout: float = 240) -> None:
    """Get back to an idle window: cancel anything running and wait."""
    if not app._running:
        pump(root, 0.3)
        return
    app.cancel_btn.invoke()
    idle = pump_until(root, lambda: not app._running, timeout)
    report.check("Error Recovery", f"returns to idle {why}", idle,
                 "cancel + wait" if idle else f"still running after {timeout}s")
    pump(root, 0.5)


# =============================================================================
# dialog stubs - a headless QA pass must never block on a modal
# =============================================================================
class Dialogs:
    """Records what the app tried to show instead of showing it."""

    def __init__(self) -> None:
        self.shown: List[Tuple[str, str]] = []
        self._saved: Dict[str, object] = {}

    def install(self) -> None:
        for name in ("showinfo", "showwarning", "showerror"):
            self._saved[name] = getattr(messagebox, name)
            setattr(messagebox, name, self._recorder(name))
        for name in ("askyesno", "askokcancel", "askretrycancel"):
            self._saved[name] = getattr(messagebox, name)
            setattr(messagebox, name, self._answer(name, False))
        for name in ("askdirectory", "asksaveasfilename", "askopenfilename"):
            self._saved[name] = getattr(filedialog, name)
            setattr(filedialog, name, self._answer(name, ""))

    def _recorder(self, kind: str):
        def stub(title=None, message=None, **kw):
            self.shown.append((kind, str(message)[:200]))
            return "ok"
        return stub

    def _answer(self, kind: str, value):
        def stub(*a, **kw):
            self.shown.append((kind, ""))
            return value
        return stub

    def restore(self) -> None:
        for name, func in self._saved.items():
            target = messagebox if hasattr(messagebox, name) else filedialog
            setattr(target, name, func)


# =============================================================================
# widget walking
# =============================================================================
INTERACTIVE = ("TButton", "Button", "TCheckbutton", "Checkbutton",
               "TRadiobutton", "Radiobutton", "TCombobox", "TEntry", "Entry")


def walk(widget, depth: int = 0) -> List[tk.Misc]:
    found = []
    for child in widget.winfo_children():
        found.append(child)
        found.extend(walk(child, depth + 1))
    return found


def describe(w: tk.Misc) -> str:
    try:
        text = w.cget("text")
    except Exception:
        text = ""
    return f"{w.winfo_class()}:{text or w.winfo_name()}"


def is_enabled(w: tk.Misc) -> bool:
    try:
        return "disabled" not in w.state()          # ttk
    except Exception:
        try:
            return str(w.cget("state")) != "disabled"
        except Exception:
            return True


# =============================================================================
# the QA pass
# =============================================================================
def qa(args) -> int:
    os.makedirs(PERF_DIR, exist_ok=True)
    report = Report()
    started_wall = datetime.now()

    print("\n=== 1. STARTUP ===", flush=True)
    t0 = time.perf_counter()
    core.register_newspapers(scrapers.load_all())
    report.check("UI", "newspaper plugins load",
                 len(core.NEWSPAPER_REGISTRY) >= 4,
                 f"{len(core.NEWSPAPER_REGISTRY)} registered")

    root = tk.Tk()
    root.geometry("1500x950+30+30")
    app = core.Application(root)
    root.update()
    startup = time.perf_counter() - t0
    report.check("UI", "window builds", True, f"{startup:.1f}s")
    report.metrics["startup_s"] = round(startup, 2)

    dialogs = Dialogs()
    dialogs.install()
    beat = Heartbeat(root)

    widgets = walk(root)
    interactive = [w for w in widgets if w.winfo_class() in INTERACTIVE]
    report.check("UI", "interactive widgets found", len(interactive) >= 15,
                 f"{len(interactive)} widgets")

    # -- 2. every button -----------------------------------------------------
    print("\n=== 2. BUTTON-BY-BUTTON ===", flush=True)
    buttons = [w for w in interactive
               if w.winfo_class() in ("TButton", "Button")]
    clicked, skipped = 0, 0
    for button in buttons:
        label = ""
        try:
            label = str(button.cget("text"))
        except Exception:
            pass
        if not button.winfo_ismapped():
            continue                       # hidden (e.g. the ☰ Log button)
        if any(label.startswith(x) for x in DO_NOT_CLICK):
            skipped += 1
            continue
        before = is_enabled(button)
        try:
            button.invoke()
            pump(root, 0.25)
            button.invoke()                # twice: repeated clicks must be safe
            pump(root, 0.25)
            clicked += 1
        except Exception as exc:
            report.check("Buttons", f"click '{label}'", False, repr(exc)[:120])
            continue
        still_there = bool(button.winfo_exists())
        report.check("Buttons", f"click '{label}' x2",
                     still_there, f"enabled={before}")
    report.check("Buttons", "every visible button clicked twice", clicked > 0,
                 f"{clicked} clicked, {skipped} skipped by policy "
                 f"({', '.join(DO_NOT_CLICK)})")

    # Clicking every button includes Extract, so a real run is very likely
    # in flight now.  Later phases measure an IDLE window, so stop it and
    # wait - otherwise "scroll before extraction" measures scrolling during
    # a half-cancelled extraction, which is a different question.
    quiesce(root, app, report, "after the button sweep")

    # -- 3. inputs, dropdowns, toggles --------------------------------------
    print("\n=== 3. INPUTS / DROPDOWNS / TOGGLES ===", flush=True)
    combos = [w for w in interactive if w.winfo_class() == "TCombobox"]
    for combo in combos:
        try:
            values = list(combo.cget("values"))
        except Exception:
            values = []
        if not values:
            continue
        original = combo.get()
        ok = True
        for value in values[:6]:
            try:
                combo.set(value)
                combo.event_generate("<<ComboboxSelected>>")
                pump(root, 0.15)
            except Exception:
                ok = False
        combo.set(original)
        combo.event_generate("<<ComboboxSelected>>")
        pump(root, 0.1)
        report.check("UI", f"dropdown {describe(combo)} accepts all values",
                     ok, f"{len(values)} values")

    # Notice type toggle - the thing that must actually change the filter.
    type_ok = True
    for label in core.NOTICE_TYPE_CHOICES:
        app.notice_type_var.set(label)
        app._on_notice_type(label)
        pump(root, 0.1)
        expected = ("chetavni" if "ચેતવણ" in label
                    else "notice" if "નોટ" in label else "all")
        if core.active_notice_type() != expected:
            type_ok = False
    app.notice_type_var.set(core.NOTICE_TYPE_CHOICES[0])
    app._on_notice_type(core.NOTICE_TYPE_CHOICES[0])
    report.check("UI", "notice-type toggle drives the matcher", type_ok,
                 f"All / નોટિસ / ચેતવણી -> {core.active_notice_type()}")

    entries = [w for w in interactive if w.winfo_class() == "TEntry"]
    entry_ok = True
    for entry in entries:
        try:
            entry.delete(0, "end")
            entry.insert(0, "QA typing test")
            pump(root, 0.05)
            if entry.get() != "QA typing test":
                entry_ok = False
            entry.delete(0, "end")
        except Exception:
            entry_ok = False
    report.check("UI", "text fields accept input", entry_ok,
                 f"{len(entries)} fields")

    # -- 4. search -----------------------------------------------------------
    print("\n=== 4. SEARCH ===", flush=True)
    corpus = ("This is a PUBLIC NOTICE about land. જાહેર નોટિસ મિલકત "
              "regarding property notice and survey number 42.")
    cases = [
        ("public", True), ("notice", True),
        ("public notice", True),                 # the multi-word case
        ("notice public", True),                 # order must not matter
        ("  public   notice  ", True),           # spacing
        ("PUBLIC NOTICE", True),                 # case
        ("જાહેર", True), ("જાહેર નોટિસ", True),
        ("જાહેર property", True),                # mixed scripts
        ("property notice land", True),          # three words
        ("public tender", False),                # one word absent
        ("zebra", False),
    ]
    for query, expected in cases:
        got = search_util.match_query(corpus, query)
        report.check("Search", f"query {query!r}", got is expected,
                     f"expected {expected}, got {got}")
    # "public notice" must not behave like "public"
    only_public = "a public meeting was held"
    report.check("Search", "'public notice' != 'public'",
                 search_util.match_query(only_public, "public")
                 and not search_util.match_query(only_public, "public notice"),
                 "single word matches, phrase does not")

    # -- 5. scrolling BEFORE extraction --------------------------------------
    print("\n=== 5. SCROLLING (before extraction) ===", flush=True)
    quiesce(root, app, report, "before the scroll baseline")
    beat.reset()
    scroll_targets = [("log", app.log_panel), ("gallery", app.gallery)]
    for name, panel in scroll_targets:
        for delta in (-120, -120, 120, 120):
            try:
                panel.event_generate("<MouseWheel>", delta=delta, x=10, y=10)
            except Exception:
                pass
        pump(root, 0.3)
    report.check("Scrolling", "scroll before extraction is smooth",
                 beat.worst_ms() < 200, f"worst stall {beat.worst_ms():.0f} ms")
    report.metrics["scroll_before_ms"] = round(beat.worst_ms(), 1)

    # -- 6. live extraction --------------------------------------------------
    print("\n=== 6. LIVE EXTRACTION ===", flush=True)
    core.PAGE_LIMIT[0] = args.pages
    if args.paper:
        app.newspaper_var.set(args.paper)
    else:
        app.newspaper_var.set(core.ALL_NEWSPAPERS_LABEL)
    app._on_newspaper_changed()
    pump(root, 0.4)

    agent_spans: List[Tuple[str, float, float]] = []
    original_extract_all = core.BaseNewspaperExtractor.extract_all

    def timed_extract_all(self, jobs, reporter, **kw):
        label = f"{self.display_name} {jobs[0][0] if jobs else ''}"
        begin = time.perf_counter()
        try:
            return original_extract_all(self, jobs, reporter, **kw)
        finally:
            agent_spans.append((label, begin, time.perf_counter()))

    core.BaseNewspaperExtractor.extract_all = timed_extract_all
    beat.reset()
    run_started = time.perf_counter()
    app.extract_btn.invoke()
    pump(root, 1.0)
    report.check("Extraction", "run starts", app._running,
                 "extract button set the running flag")

    # Scroll and click WHILE it runs - this is the freeze test.
    print("  ... running; scrolling and clicking during extraction",
          flush=True)
    during_worst = 0.0
    while app._running and time.perf_counter() - run_started < args.timeout:
        for name, panel in scroll_targets:
            for delta in (-120, 120):
                try:
                    panel.event_generate("<MouseWheel>", delta=delta,
                                         x=10, y=10)
                except Exception:
                    pass
        pump(root, 0.5)
        during_worst = max(during_worst, beat.worst_ms())
    total_run = time.perf_counter() - run_started
    core.BaseNewspaperExtractor.extract_all = original_extract_all

    finished = not app._running
    report.check("Extraction", "run completes",
                 finished, f"{total_run:.1f}s")
    report.check("UI Responsiveness",
                 "UI stays responsive during extraction",
                 during_worst < 400,
                 f"worst stall {during_worst:.0f} ms "
                 f"(median tick {beat.median_ms():.0f} ms)")
    report.metrics["ui_stall_during_ms"] = round(during_worst, 1)
    report.metrics["total_run_s"] = round(total_run, 1)

    notices = len(app.gallery.all_results())
    # One or two pages of a paper legitimately hold no notice - the front
    # page usually does not - so this is only a failure when the run was
    # deep enough that finding nothing anywhere would be suspicious.
    if args.pages >= 3:
        report.check("Extraction", "notices detected", notices > 0,
                     f"{notices} notices over {args.pages} pages/edition")
    else:
        print(f"  [info] {notices} notices ({args.pages} page(s) per "
              f"edition - too shallow to assert on)", flush=True)
    report.metrics["notices"] = notices

    # -- 7. parallel agents: prove overlap with timestamps -------------------
    print("\n=== 7. PARALLEL AGENTS ===", flush=True)
    overlap = 0
    for i, (label_a, a0, a1) in enumerate(agent_spans):
        for label_b, b0, b1 in agent_spans[i + 1:]:
            if min(a1, b1) - max(a0, b0) > 0.5:
                overlap += 1
    for label, begin, end in sorted(agent_spans, key=lambda s: s[1]):
        print(f"    {label:<34} "
              f"{begin - run_started:6.1f}s -> {end - run_started:6.1f}s",
              flush=True)
    report.check("Parallel Agents", "agents genuinely overlap in time",
                 overlap > 0 if len(agent_spans) > 1 else True,
                 f"{len(agent_spans)} agents, {overlap} overlapping pairs")
    report.metrics["agents"] = len(agent_spans)
    report.metrics["overlapping_pairs"] = overlap

    # -- 8. scrolling AFTER + log integrity ----------------------------------
    print("\n=== 8. SCROLLING (after) + LOG ===", flush=True)
    beat.reset()
    for name, panel in scroll_targets:
        for delta in (-120,) * 8 + (120,) * 8:
            try:
                panel.event_generate("<MouseWheel>", delta=delta, x=10, y=10)
            except Exception:
                pass
        pump(root, 0.3)
    report.check("Scrolling", "scroll after extraction is smooth",
                 beat.worst_ms() < 200, f"worst stall {beat.worst_ms():.0f} ms")
    report.metrics["scroll_after_ms"] = round(beat.worst_ms(), 1)

    text = app.log_panel._text
    line_count = int(text.index("end-1c").split(".")[0])
    report.check("Scrolling", "log holds the run's lines", line_count > 5,
                 f"{line_count} lines on screen")
    text.yview_moveto(1.0)
    pump(root, 0.2)
    report.check("Scrolling", "newest log line reachable",
                 text.yview()[1] > 0.99, "scrolled to bottom")

    # -- 9. cancellation -----------------------------------------------------
    print("\n=== 9. CANCELLATION ===", flush=True)
    core.PAGE_LIMIT[0] = max(4, args.pages)
    app.extract_btn.invoke()
    pump(root, 2.0)
    was_running = app._running
    app.cancel_btn.invoke()
    stopped = pump_until(root, lambda: not app._running, timeout=60)
    report.check("Error Recovery", "cancel stops a running extraction",
                 was_running and stopped,
                 f"started={was_running}, stopped={stopped}")

    # -- 10. error state: a bad URL must fail cleanly ------------------------
    print("\n=== 10. ERROR HANDLING ===", flush=True)
    dialogs.shown.clear()
    app.newspaper_var.set(core.NEWSPAPER_REGISTRY and
                          list(core.NEWSPAPER_REGISTRY)[0])
    app._on_newspaper_changed()
    app.url_var.set("https://example.invalid/not-an-epaper/1")
    app.extract_btn.invoke()
    ended = pump_until(root, lambda: not app._running, timeout=90)
    complained = bool(dialogs.shown) or bool(report)
    report.check("Error Recovery", "bad URL fails without hanging",
                 ended, f"returned to idle={ended}, dialogs={len(dialogs.shown)}")
    report.check("Error Recovery", "app still usable after a failure",
                 bool(root.winfo_exists()) and is_enabled(app.extract_btn),
                 "extract button re-enabled")

    # -- 11. resize ----------------------------------------------------------
    print("\n=== 11. RESIZE ===", flush=True)
    beat.reset()
    for geom in ("1100x700+30+30", "1600x1000+10+10", "900x600+50+50",
                 "1500x950+30+30"):
        root.geometry(geom)
        pump(root, 0.4)
    report.check("UI", "window survives resizing", bool(root.winfo_exists()),
                 f"worst stall {beat.worst_ms():.0f} ms")

    beat.stop()
    dialogs.restore()
    root.destroy()

    # -- 12. performance history --------------------------------------------
    record = {
        "run_id": 0,
        "when": started_wall.isoformat(timespec="seconds"),
        "pages_per_edition": args.pages,
        "paper": args.paper or "all",
        **report.metrics,
        "checks": len(report.checks),
        "failures": len(report.failures),
    }
    history = []
    if os.path.exists(RUNS_FILE):
        try:
            with open(RUNS_FILE, encoding="utf-8") as fh:
                history = json.load(fh)
        except (OSError, ValueError):
            history = []
    record["run_id"] = len(history) + 1
    previous = history[-1] if history else None
    history.append(record)
    with open(RUNS_FILE, "w", encoding="utf-8") as fh:
        json.dump(history, fh, indent=2)

    print_report(report, record, previous)
    return 1 if report.failures else 0


def print_report(report: Report, record: dict,
                 previous: Optional[dict]) -> None:
    sections = ["UI", "Buttons", "Search", "Extraction", "Parallel Agents",
                "Scrolling", "UI Responsiveness", "Error Recovery"]
    print("\n" + "=" * 52)
    print("QA REPORT")
    print("=" * 52)
    print(f"Application : {core.APP_TITLE}")
    print(f"Run         : #{record['run_id']}  {record['when']}")
    print(f"Scope       : {record['paper']}, "
          f"{record['pages_per_edition']} page(s) per edition")
    print("-" * 52)
    for section in sections:
        print(f"{section:<22}{report.section_status(section)}")
    print("-" * 52)
    print("Performance")
    print(f"  total run           {record.get('total_run_s', '?')} s")
    print(f"  notices found       {record.get('notices', '?')}")
    print(f"  agents / overlaps   {record.get('agents', '?')} / "
          f"{record.get('overlapping_pairs', '?')}")
    print(f"  UI stall (running)  {record.get('ui_stall_during_ms', '?')} ms")
    print(f"  scroll before/after {record.get('scroll_before_ms', '?')} / "
          f"{record.get('scroll_after_ms', '?')} ms")
    if previous and previous.get("total_run_s") and record.get("total_run_s"):
        before, after = previous["total_run_s"], record["total_run_s"]
        delta = (before - after) / before * 100
        print(f"  previous run        {before} s")
        print(f"  change              {delta:+.1f}%  "
              f"(notices {previous.get('notices')} -> {record.get('notices')})")
    print("-" * 52)
    if report.failures:
        print(f"FAILED ({len(report.failures)})")
        for section, name, _ok, detail in report.failures:
            print(f"  X {section}: {name}")
            if detail:
                print(f"      {detail}")
    else:
        print(f"All {len(report.checks)} checks passed.")
    print("=" * 52)
    print(f"history: {RUNS_FILE}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=2,
                        help="pages per edition during the extraction test")
    parser.add_argument("--paper", default="",
                        help="restrict to one newspaper")
    parser.add_argument("--timeout", type=float, default=900,
                        help="seconds to allow the extraction test")
    return qa(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
