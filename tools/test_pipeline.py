#!/usr/bin/env python3
"""Self-checks for the parts of the extractor that are easy to get wrong.

    python test_pipeline.py

Covers the live-streaming gallery routing, the OCR backend chain, and the
agent-level retry logic.  No network, no newspaper site, no GUI window is
ever shown (Tk is created withdrawn).  Plain asserts - no test framework.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import threading
import time
import tkinter as tk

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


def load():
    """The app core.  A normal import (not a file-path load): the scrapers
    import it too, and two module objects would mean two registries."""
    from notice_extractor import core
    return core


pne = load()

# One Tk root for the whole run.  Creating and destroying several of them in
# one process makes Tcl abort at exit ("async handler deleted by the wrong
# thread"), which kills the buffered test output along with it.
_ROOT = None


def tk_root():
    global _ROOT
    if _ROOT is None:
        _ROOT = tk.Tk()
        _ROOT.withdraw()
    return _ROOT


def new_gallery():
    return pne.GalleryPanel(tk_root(), on_open=lambda r: None,
                            on_save=lambda r: None, on_click=lambda r: None)


def make_result(pne, paper, page, section=""):
    return pne.NoticeResult(
        result_id=0, page_number=page, index_on_page=1,
        image_bgr=pne.np.zeros((8, 8, 3), dtype=pne.np.uint8),
        confidence=90, method="test", newspaper=paper,
        section_title=section)


# ---------------------------------------------------------------------------
def test_gallery_routes_interleaved_results():
    """The whole point of live streaming: three agents publishing at once
    must not have their notices filed under each other's headings."""
    gallery = new_gallery()
    for title in ("Paper A", "Paper B", "Paper C"):
        gallery.add_heading(title)
    assert len(gallery.sections) == 3, gallery.sections

    # Arrive out of order, exactly as parallel agents would deliver them.
    for paper, page in (("Paper B", 1), ("Paper A", 1), ("Paper C", 1),
                        ("Paper B", 2), ("Paper A", 2), ("Paper B", 3)):
        gallery.add_result(make_result(pne, paper, page, section=paper))

    by_title = {s["title"]: s["results"] for s in gallery.sections}
    assert len(by_title["Paper A"]) == 2, by_title["Paper A"]
    assert len(by_title["Paper B"]) == 3, by_title["Paper B"]
    assert len(by_title["Paper C"]) == 1, by_title["Paper C"]
    assert len(gallery.results) == 6
    # Every notice landed under its own paper.
    for section in gallery.sections:
        for res in section["results"]:
            assert res.newspaper == section["title"], (
                res.newspaper, section["title"])
    print("ok  gallery routes interleaved results to the right sections")


def test_gallery_creates_section_on_demand():
    """A result whose heading has not been emitted yet still gets its own
    section instead of landing in whatever was last."""
    gallery = new_gallery()
    gallery.add_heading("Known")
    gallery.add_result(make_result(pne, "Surprise", 1, section="Surprise"))
    titles = [s["title"] for s in gallery.sections]
    assert titles == ["Known", "Surprise"], titles
    assert len(gallery.sections[0]["results"]) == 0
    assert len(gallery.sections[1]["results"]) == 1
    print("ok  gallery creates a missing section on demand")


# ---------------------------------------------------------------------------
class _Recorder:
    def __init__(self):
        self.results = []
        self.logs = []

    def check_cancel(self):
        pass

    def log(self, text, level="info"):
        self.logs.append((level, text))

    def result(self, res):
        self.results.append(res)


def test_reporter_streams_immediately():
    """BufferedJobReporter must forward a notice the moment it is cropped -
    if it only buffered, the gallery would stay empty until the agent ends."""
    base = _Recorder()
    reporter = pne.BufferedJobReporter(base, "GS 10-08", "GS - ahd - 10-08")
    reporter.result(make_result(pne, "Gujarat Samachar", 3))
    assert len(base.results) == 1, "notice was not forwarded live"
    assert base.results[0].section_title == "GS - ahd - 10-08"
    assert len(reporter.collected) == 1, "count bookkeeping lost"
    print("ok  reporter streams each notice immediately")


def test_agent_retries_transient_failures_but_never_credentials():
    """The retry rule this file's own docstring has always claimed to cover.

    Three separate promises in agents/processor.py:
      * a transient failure is retried, and the run still produces notices,
      * an AUTH: failure is NOT retried - a wrong password will still be
        wrong the third time, and each attempt is a fresh browser launch,
      * notices an agent published BEFORE it died are salvaged into the
        total, or the summary says "3 notices" under a screen showing 57.
    """
    import queue as queue_mod
    import threading as threading_mod

    from notice_extractor.agents import processor

    day = pne.date.today()

    def run(extractor_cls):
        reporter = pne.ProgressReporter(queue_mod.Queue(),
                                        threading_mod.Event())
        return processor.run_jobs(
            [(extractor_cls, "ahmedabad", day, "https://example.test/1")],
            reporter)

    class _Flaky:
        """Uses up every retry with transient errors, then works.

        Driven off AGENT_RETRIES rather than a hardcoded count, so turning
        the knob down to 0 fails this test loudly instead of quietly
        weakening it."""
        display_name = "Flaky Paper"
        attempts = 0

        def __init__(self, broad=False):
            self.current_issue_date = ""

        def extract_all(self, jobs, reporter, **kwargs):
            type(self).attempts += 1
            if type(self).attempts <= pne.AGENT_RETRIES:
                raise pne.ExtractionError("connection reset by peer")
            reporter.result(make_result(pne, "Flaky Paper", 3))

    class _BadPassword:
        """Fails with credentials, which must be believed the first time."""
        display_name = "Locked Paper"
        attempts = 0

        def __init__(self, broad=False):
            self.current_issue_date = ""

        def extract_all(self, jobs, reporter, **kwargs):
            type(self).attempts += 1
            raise pne.ExtractionError("AUTH: sign-in required")

    class _DiesAfterPublishing:
        """Publishes two notices, then dies for good."""
        display_name = "Half Paper"
        attempts = 0

        def __init__(self, broad=False):
            self.current_issue_date = ""

        def extract_all(self, jobs, reporter, **kwargs):
            type(self).attempts += 1
            reporter.result(make_result(pne, "Half Paper", 1))
            reporter.result(make_result(pne, "Half Paper", 2))
            raise pne.ExtractionError("stream closed mid-edition")

    assert pne.AGENT_RETRIES >= 1, "no retries configured at all"
    summary = run(_Flaky)
    assert _Flaky.attempts == pne.AGENT_RETRIES + 1, \
        f"retried {_Flaky.attempts - 1} of {pne.AGENT_RETRIES} time(s)"
    assert summary.total == 1, summary.total
    assert not summary.skipped, summary.skipped

    summary = run(_BadPassword)
    assert _BadPassword.attempts == 1, \
        f"credentials retried {_BadPassword.attempts} times"
    assert summary.total == 0 and summary.skipped

    summary = run(_DiesAfterPublishing)
    assert _DiesAfterPublishing.attempts == pne.AGENT_RETRIES + 1
    # Published before it died - and published notices are on screen.
    assert summary.total == 2, \
        f"lost {2 - summary.total} notice(s) the user can see"
    assert summary.per_paper.get("Half Paper") == 2, summary.per_paper
    print(f"ok  agents retry transient failures ({pne.AGENT_RETRIES} "
          f"retr{'y' if pne.AGENT_RETRIES == 1 else 'ies'}), never "
          "credentials, and keep what they published")


# ---------------------------------------------------------------------------
def test_ocr_chain_prefers_gujarati():
    """The chain must skip a Latin-only engine and keep walking, and must
    return None rather than hand back an engine that cannot read Gujarati."""
    logs = []

    def log(text, level="info"):
        logs.append(text)

    class Fake:
        def __init__(self, name, guj):
            self.name = name
            self.supports_gujarati = guj

    original = (pne.WindowsOcrEngine.create, pne.TesseractOcrEngine.create,
                pne.EasyOcrEngine.create)
    try:
        # Latin-only first rung, Gujarati second -> second must win.
        pne.WindowsOcrEngine.create = staticmethod(
            lambda: Fake("Windows OCR", False))
        pne.TesseractOcrEngine.create = staticmethod(
            lambda: Fake("Tesseract", True))
        pne.EasyOcrEngine.create = staticmethod(lambda: None)
        engine = pne._build_ocr_engine(log)
        assert engine is not None and engine.name == "Tesseract", engine
        assert any("Using Tesseract" in t for t in logs), logs

        # Nothing Gujarati-capable -> None, so detection knows it is blind.
        pne.TesseractOcrEngine.create = staticmethod(lambda: None)
        assert pne._build_ocr_engine(log) is None

        # A broken backend must not abort the walk.
        def boom():
            raise RuntimeError("driver exploded")

        pne.WindowsOcrEngine.create = staticmethod(boom)
        pne.TesseractOcrEngine.create = staticmethod(
            lambda: Fake("Tesseract", True))
        engine = pne._build_ocr_engine(log)
        assert engine is not None and engine.name == "Tesseract"
    finally:
        (pne.WindowsOcrEngine.create, pne.TesseractOcrEngine.create,
         pne.EasyOcrEngine.create) = original
    print("ok  ocr chain skips non-Gujarati backends and survives a crash")


def test_ocr_engine_is_cached():
    """Every edition agent asks for an engine; it must be built once."""
    pne.reset_ocr_engine_cache()
    calls = []

    class Fake:
        name = "Fake"
        supports_gujarati = True

    original = pne.WindowsOcrEngine.create
    try:
        def counted():
            calls.append(1)
            return Fake()

        pne.WindowsOcrEngine.create = staticmethod(counted)

        rec = _Recorder()
        engines = []
        threads = [threading.Thread(
            target=lambda: engines.append(pne.select_ocr_engine(rec)))
            for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) == 1, f"engine built {len(calls)} times, want 1"
        assert all(e is engines[0] for e in engines), "agents got different engines"
    finally:
        pne.WindowsOcrEngine.create = original
        pne.reset_ocr_engine_cache()
    print("ok  ocr engine is built once and shared by all agents")


# ---------------------------------------------------------------------------
def test_detect_gate_bounds_concurrency():
    """The gate is what stops N edition agents starting N full-page template
    sweeps at once."""
    width = pne.DETECT_CONCURRENCY
    assert width >= 2, width
    live = {"cur": 0, "max": 0}
    lock = threading.Lock()
    threads = []

    def worker():
        with pne._detect_gate:
            with lock:
                live["cur"] += 1
                live["max"] = max(live["max"], live["cur"])
            # Long enough that the holders genuinely overlap - a busy loop
            # that finishes instantly would make this test pass even with a
            # gate of width 1.
            time.sleep(0.05)
            with lock:
                live["cur"] -= 1

    for _ in range(width * 3):
        thread = threading.Thread(target=worker, daemon=True)
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert live["max"] <= width, f"{live['max']} concurrent detects > {width}"
    assert live["max"] > 1, "gate serialised everything - no parallelism"
    print(f"ok  detect gate allowed {live['max']} concurrent detects, "
          f"capped at {width}")


def test_detect_gate_release_round_trips():
    """OCR hands the detect slot back mid-page.  Over-releasing a
    BoundedSemaphore raises, so this must balance exactly - and it must be a
    no-op for a thread that never took a slot."""
    width = pne.DETECT_CONCURRENCY

    # Safe outside a held gate.
    with pne.detect_gate_released():
        pass

    with pne.detect_gate_held():
        # One slot is taken, so exactly width-1 more can be grabbed.
        taken = [pne._detect_gate.acquire(blocking=False)
                 for _ in range(width)]
        assert taken.count(True) == width - 1, taken
        for got in taken:
            if got:
                pne._detect_gate.release()

        # While released, the full width must be available again.
        with pne.detect_gate_released():
            inner = [pne._detect_gate.acquire(blocking=False)
                     for _ in range(width)]
            assert all(inner), f"slot not handed back: {inner}"
            for _ in inner:
                pne._detect_gate.release()

    # Back to full width, and no over-release happened.
    after = [pne._detect_gate.acquire(blocking=False) for _ in range(width)]
    assert all(after), f"gate leaked a slot: {after}"
    for _ in after:
        pne._detect_gate.release()
    print("ok  detect gate release round-trips without leaking a slot")


def test_newspaper_plugins_load():
    """Every file in scrapers/ must register an extractor, and the core
    must be able to resolve a URL to one."""
    from notice_extractor import scrapers as newspapers

    registry = newspapers.load_all()
    problems = newspapers.errors()
    assert not problems, f"plugins failed to import: {problems}"
    assert len(registry) >= 5, f"only loaded {list(registry)}"

    for name, cls in registry.items():
        assert cls.display_name == name, (name, cls.display_name)
        # The contract every plugin has to satisfy.
        for hook in ("matches", "build_url", "discover", "fetch_page",
                     "pipeline_cls"):
            assert hasattr(cls, hook), f"{name} is missing {hook}"

    pne.register_newspapers(registry)
    found = pne.find_extractor_for_url(
        "https://epaper.gujaratsamachar.com/ahmedabad/10-08-2026/1")
    assert found is not None and found.display_name == "Gujarat Samachar", found
    assert pne.local_pdf_extractor() is not None
    print(f"ok  {len(registry)} newspaper plugins loaded and resolvable")


def test_plugin_loader_is_idempotent():
    """load_all() is called from more than one place; the import work must
    happen once and always return the same classes."""
    from notice_extractor import scrapers as newspapers

    first = newspapers.load_all()
    second = newspapers.load_all()
    assert first == second, "loader returned different registries"
    assert all(first[k] is second[k] for k in first), "classes were re-imported"
    print("ok  plugin loader is idempotent")


def test_plugins_have_no_dangling_names():
    """Every global a scraper reads must be defined in it or in core.py.

    Splitting the old single file left three plugins calling pdf_render_page,
    which had moved into a sibling plugin - the app only found out mid-run,
    26 pages in.  Static check so that class of break cannot come back."""
    import ast
    import builtins

    def toplevel_names(tree):
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
            elif isinstance(node, ast.Global):
                names.update(node.names)
        return names

    core_src = open(os.path.join(ROOT, "notice_extractor", "core.py"),
                    encoding="utf-8").read()
    core_tree = ast.parse(core_src)
    known = toplevel_names(core_tree) | set(dir(builtins))

    # The core must also be self-contained: moving pdf_render_page over left
    # its PDF_RENDER_WIDTH constant behind in the plugin, and the plugin-only
    # scan below was blind to it.
    core_missing = {}
    for node in ast.walk(core_tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id not in known:
            core_missing.setdefault(node.id, node.lineno)
    assert not core_missing, f"core.py uses undefined names: {core_missing}"

    folder = os.path.join(ROOT, "notice_extractor", "scrapers")
    checked = 0
    for filename in sorted(os.listdir(folder)):
        if not filename.endswith(".py") or filename == "__init__.py":
            continue
        tree = ast.parse(open(os.path.join(folder, filename),
                              encoding="utf-8").read())
        local = toplevel_names(tree)
        missing = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                    and node.id not in local and node.id not in known:
                missing.setdefault(node.id, node.lineno)
        assert not missing, f"{filename} uses undefined names: {missing}"
        checked += 1
    print(f"ok  {checked} plugins reference no undefined names")


def test_search_finds_and_boxes_gujarati():
    """Typing Gujarati into Find must locate it inside a crop and return the
    word's box, so the card can paint the blue selection over it."""
    engine = pne.select_ocr_engine(_Recorder())
    if engine is None or not engine.supports_gujarati:
        print("skip  search test (no Gujarati OCR backend on this machine)")
        return

    class Quiet:
        def log(self, *a, **k):
            pass

        def check_cancel(self):
            pass

    fonts = pne.discover_gujarati_fonts(max_fonts=1)
    assert fonts, "no Gujarati font available to render a test crop"
    verifier = pne.HeaderTemplateVerifier(pne.DETECTION_CONFIG, Quiet())
    rendered = verifier._render_text("જાહેર નોટીસ", fonts[0])
    assert rendered is not None
    crop = pne.cv2.cvtColor(rendered, pne.cv2.COLOR_GRAY2BGR)

    app = pne.Application(tk_root())
    result = make_result(pne, "Test", 1, section="Test")
    result.image_bgr = crop
    app.gallery.add_heading("Test")
    app.gallery.add_result(result)

    def wait():
        for _ in range(160):
            tk_root().update()
            time.sleep(0.25)
            if not app._searching:
                return
        raise AssertionError("search never finished")

    app.start_search("નોટીસ")
    wait()
    assert result.ocr_done and result.ocr_text.strip(), result.ocr_text
    assert result.match_boxes, f"no box for a word that reads {result.ocr_text!r}"
    x, y, w, h = result.match_boxes[0]
    assert w > 0 and h > 0 and 0 <= x < crop.shape[1], result.match_boxes

    # English must work too - the engine's lang has to carry eng alongside
    # guj (a guj-only tessdata redirect silently dropped English once).
    assert "eng" in getattr(engine, "lang", ""), (
        f"engine lang {getattr(engine, 'lang', None)!r} has no English")
    crop_en = pne.np.full((90, 560, 3), 255, dtype=pne.np.uint8)
    pne.cv2.putText(crop_en, "PUBLIC NOTICE 77", (12, 60),
                    pne.cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 0), 3)
    result_en = make_result(pne, "TestEn", 1, section="TestEn")
    result_en.image_bgr = crop_en
    app.gallery.add_heading("TestEn")
    app.gallery.add_result(result_en)
    app.start_search("NOTICE")
    wait()
    assert result_en.match_boxes, (
        f"English search found nothing; read {result_en.ocr_text!r}")

    # A miss must clear the highlight rather than leave the previous one up.
    app.start_search("ZZZNOTFOUND")
    wait()
    assert not result.match_boxes and not result_en.match_boxes
    print("ok  search finds Gujarati AND English text with word boxes")


def test_notice_type_toggle_filters_matching():
    """The All / જાહેર નોટિસ / જાહેર ચેતવણી toggle must gate keyword
    matching AND the rendered templates."""

    class Quiet:
        def log(self, *a, **k):
            pass

        def check_cancel(self):
            pass

    try:
        pne.set_notice_type("જાહેર ચેતવણી")
        assert pne.match_notice_text("જાહેર ચેતવણી", False)[0] > 0
        assert pne.match_notice_text("જાહેર નોટિસ", False)[0] == 0, \
            "chetavni-only mode still matches નોટિસ"
        # The embedded crops are filtered by TYPE, not dropped wholesale:
        # chetavni mode keeps the real ચેતવણી crop and drops every નોટિસ
        # one.  (It used to drop them all, which was right only while every
        # embedded crop was a નોટિસ.)
        verifier = pne.HeaderTemplateVerifier(pne.DETECTION_CONFIG, Quiet())
        families = verifier.embedded_families
        assert "notice" not in families, \
            "chetavni-only mode kept the નોટિસ embedded templates"
        assert families == {"chetavni"} or not families, \
            f"unexpected embedded families in chetavni mode: {families}"

        pne.set_notice_type("જાહેર નોટિસ")
        assert pne.match_notice_text("જાહેર નોટિસ", False)[0] > 0
        assert pne.match_notice_text("જાહેર ચેતવણી", False)[0] == 0

        pne.set_notice_type("All")
        assert pne.match_notice_text("જાહેર નોટિસ", False)[0] > 0
        assert pne.match_notice_text("જાહેર ચેતવણી", False)[0] > 0
    finally:
        pne.set_notice_type("All")
    print("ok  notice-type toggle gates keywords and templates")


def test_multi_word_search_matches_every_token():
    """"public notice" used to find nothing: the query was normalised to
    "publicnotice" (normalisation strips spaces) and then looked for inside
    single OCR words, which never contain it.  Token search, no OCR needed."""
    from notice_extractor.utils import search

    search.demo()          # the module's own asserts, incl. Gujarati

    class W:
        def __init__(self, text, x):
            self.text, self.x, self.y, self.w, self.h = text, x, 4, 30, 12

    text = "PUBLIC NOTICE is hereby given"
    words = [W(t, i * 40) for i, t in enumerate(text.split())]
    hit, boxes = search.search_notice(words, text, "public notice")
    assert hit and len(boxes) == 2, (hit, boxes)
    assert not search.search_notice(words, text, "public auction")[0]
    # Reversed order and stray punctuation are the same query.
    assert search.match_query(text, "notice, public!")
    print("ok  multi-word search matches every token, in any order")


def test_search_result_flag_drives_the_count():
    """A notice whose OCR glued the phrase into one blob still counts as a
    match - the gallery counts the flag, not len(boxes)."""
    from notice_extractor.utils import search

    class W:
        text, x, y, w, h = "PUBLICNOTICE", 0, 0, 90, 12

    hit, boxes = search.search_notice([W()], "PUBLICNOTICE", "public notice")
    assert hit, "glued OCR text lost the match"
    assert boxes, "the glued word should still be highlighted"
    print("ok  a match without separate words is still a match")


def test_target_date_resolves():
    """config.TARGET_DATE: 'auto' means today, an ISO date pins the run, and
    a typo must not take a scheduled run down."""
    from datetime import date

    from notice_extractor import config

    assert config.resolve_target_date() == date.today()
    assert config.resolve_target_date("2026-08-08") == date(2026, 8, 8)
    assert config.resolve_target_date("not-a-date") == date.today()
    print("ok  target date resolves (auto / pinned / bad value)")


def test_divya_bhaskar_page_list_parsing():
    """The page list and the access token must be read out of the viewer's
    embedded state - the shape the live site actually serves."""
    import json

    db = pne.newspaper_module("divya_bhaskar")
    token = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
    base = ("https://epaper.bhaskarassets.com/thumb/0x0/db-epaper-digital/"
            "10082026/gujarat/")
    state = {"props": {"initialState": {"common": {"epaperDetail": {
        "edCode": "12", "edDate": "2026-08-10",
        "pgs": [{"pg": "J1", "imgH": base + "aaa", "imgUH": base + "aaa"},
                {"pg": "1", "imgH": base + "bbb", "imgUH": base + "bbb"}],
        "ss": {"subsData": {"pt": token}}}}}}}
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(state) + "</script>")

    detail = db._db_epaper_detail(db._db_next_data(html))
    assert detail.get("edCode") == "12", detail
    assert db._db_pt_from_detail(detail) == token

    pages = db._db_pages_from_json(detail, "https://x/detail-page/a/12/d")
    assert len(pages) == 2, pages
    assert pages[0].page_number == 1 and pages[1].page_number == 2
    # /thumb/0x0/ is the full-size render: upscaling it would SHRINK it.
    assert pages[0].image_url == base + "aaa" + db.DB_IMAGE_SUFFIX
    assert "/thumb/0x0/" in pages[0].image_url

    # A logged-out page carries the shell but no list.
    assert db._db_pages_from_json(
        db._db_epaper_detail(db._db_next_data(
            '<script id="__NEXT_DATA__">{"props":{}}</script>')), "u") == []
    print("ok  divya bhaskar page list + access token parse")


def test_divya_bhaskar_login_detection():
    """An anonymous visit sets 'pt' and 'UID'.  Treating those as a login is
    what made a fresh browser profile skip signing in."""
    db = pne.newspaper_module("divya_bhaskar")
    anonymous = "UID=abc; XID=def; pt=xyz; _ga=GA1.3.1"
    signed_in = "UID=abc; dbskrat=tok; dbskrrt=tok2; dbskruid=9"
    assert not db._db_cookie_is_logged_in(anonymous)
    assert db._db_cookie_is_logged_in(signed_in)
    assert db._db_cookie_is_logged_in("at=1; rt=2")
    assert not db._db_cookie_is_logged_in("")
    print("ok  divya bhaskar tells a login from an anonymous visit")


def test_status_log_pane_stays_visible():
    """The Status Log used to come back at zero width after being closed and
    re-opened - "open" but invisible.  It must keep a usable width on the
    first layout and after every toggle."""
    from notice_extractor.ui.app import LOG_PANE_MIN_WIDTH

    root = tk_root()
    # The one test that has to map a window: an unmapped paned window has no
    # width, so "is the pane visible?" cannot be asked of it.
    root.geometry("1400x860+60+60")
    root.deiconify()
    app = pne.Application(root)

    def settle(seconds=1.0):
        end = time.time() + seconds
        while time.time() < end:
            root.update()
            time.sleep(0.02)

    try:
        settle()
        # It now starts CLOSED (see
        # test_status_log_starts_closed_and_comes_back_whole); this test is
        # about the width it comes back at, so open it first.
        assert not app._log_visible, "the log opened by default"
        app.toggle_log()
        settle(0.8)
        assert app._log_visible
        first = app._paned.sashpos(0)
        assert first >= LOG_PANE_MIN_WIDTH, f"log pane opened at {first}px"

        app.toggle_log()                      # collapse
        settle(0.4)
        assert not app._log_visible
        app.toggle_log()                      # re-open
        settle(0.8)
        again = app._paned.sashpos(0)
        assert again >= LOG_PANE_MIN_WIDTH, \
            f"re-opened log pane is only {again}px wide"

        # And it still logs, with the newest line in view.
        for index in range(80):
            app.log_panel.log(f"line {index}", "dim")
        settle(0.3)
        assert app.log_panel._at_bottom(), "the log did not follow its tail"
    finally:
        root.withdraw()
    print(f"ok  status log pane stays visible ({first}px open, {again}px "
          "after toggling)")


def test_gallery_layout_is_coalesced():
    """A run's worth of notices must cost ONE masonry pass, not one per
    notice - each pass forces a full geometry update over every card, which
    is what made the window crawl once the results arrived."""
    root = tk_root()
    root.geometry("1200x800+60+60")
    root.deiconify()
    gallery = new_gallery()
    gallery.pack(fill="both", expand=True)
    passes = []
    original = pne.GalleryPanel._layout_now
    pne.GalleryPanel._layout_now = (
        lambda self: (passes.append(1), original(self))[1])

    def settle(seconds=0.6):
        end = time.time() + seconds
        while time.time() < end:
            root.update()
            time.sleep(0.01)

    try:
        settle(0.3)
        gallery.add_heading("Perf")
        passes.clear()
        for index in range(40):
            gallery.add_result(make_result(pne, "Perf", index,
                                           section="Perf"))
        settle(0.6)
        assert len(passes) <= 2, f"{len(passes)} layout passes for 40 notices"
        assert len(gallery.cards) == 40, len(gallery.cards)

        # A height-only change cannot move a card, so it must not relayout.
        passes.clear()
        gallery._on_canvas_resize(type("E", (), {"width": gallery._last_width})())
        settle(0.2)
        assert not passes, "a height-only resize triggered a relayout"
    finally:
        pne.GalleryPanel._layout_now = original
        gallery.destroy()
        root.withdraw()
    print("ok  gallery coalesces 40 notices into one layout pass")


def test_notice_type_buttons_apply_on_click():
    """Clicking જાહેર નોટિસ / જાહેર ચેતવણી must take effect there and then,
    and show that it did - the old dropdown looked inert until Extract."""
    app = pne.Application(tk_root())
    try:
        assert len(app._type_buttons) == len(pne.NOTICE_TYPE_CHOICES)
        for choice, expected in zip(pne.NOTICE_TYPE_CHOICES,
                                    ("all", "notice", "chetavni")):
            app.notice_type_var.set(choice)
            app._on_notice_type(choice)
            assert pne.active_notice_type() == expected, (choice, expected)
            assert "✓" in app._type_hint.cget("text"), choice
            selected = [b for b in app._type_buttons
                        if "selected" in b.state()]
            assert len(selected) == 1, [b.state() for b in app._type_buttons]
    finally:
        pne.set_notice_type("All")
    print("ok  notice-type buttons apply the filter on click")


def test_setup_components_are_all_probeable():
    """The Downloads window is only useful if every component can say what
    it is, how to get it by hand, and whether it is here."""
    components = pne.setup_components()
    keys = [c.key for c in components]
    assert len(set(keys)) == len(keys), keys
    for name in ("pip", "browser", "tesseract", "traineddata", "winocr"):
        assert name in keys, keys
    for component in components:
        ready, detail = component.probe()          # must not raise
        assert isinstance(ready, bool) and detail, component.key
        assert component.instructions and component.detail, component.key
        assert component.install is not None, component.key
    required = [c.key for c in components if c.required]
    assert set(required) == {"pip", "browser", "tesseract", "traineddata"}, \
        required
    print(f"ok  {len(components)} setup components probe and describe "
          "themselves")


def test_run_data_is_transient_but_the_login_is_not():
    """Closing the app must take the run's leftovers with it - and must NOT
    take the stored login, or the sign-in-once automation becomes
    sign-in-every-time."""
    from notice_extractor import config

    made_dirs, made_files = [], []
    try:
        for name in config.TRANSIENT_DIRS:
            path = os.path.join(config.DATA_DIR, name)
            if not os.path.isdir(path):
                os.makedirs(path, exist_ok=True)
                made_dirs.append(path)
            with open(os.path.join(path, "probe.tmp"), "w") as fh:
                fh.write("x")
        keeper = os.path.join(config.DATA_DIR, config.PERSISTENT_NAMES[1])
        if not os.path.exists(keeper):
            with open(keeper, "w") as fh:
                fh.write("")
            made_files.append(keeper)

        config.clear_run_data()
        # The probe files must be gone.  Asserting the FOLDERS vanish would
        # be flaky: a parallel headless run holds its own log file open, and
        # Windows will not delete that - the folder then survives with one
        # file in it, which is correct behaviour, not a failure.
        for name in config.TRANSIENT_DIRS:
            probe = os.path.join(config.DATA_DIR, name, "probe.tmp")
            assert not os.path.exists(probe), probe
        assert os.path.exists(keeper), "clearing run data ate the login"

        # Saving into data/ has to be refused, not silently deleted later.
        assert config.is_inside_data(os.path.join(config.DATA_DIR, "out"))
        assert not config.is_inside_data(os.path.expanduser("~"))
    finally:
        for path in made_files:
            try:
                os.remove(path)
            except OSError:
                pass
    print("ok  run data is cleared on exit, the stored login survives")


def test_tessdata_is_found_wherever_it_lives():
    """tessdata/ can sit beside the package or inside it.  Whichever holds
    the model must win: if this lookup misses, Gujarati OCR silently turns
    off and detection just quietly finds less - no error, no warning."""
    import os

    from notice_extractor import config

    folder = pne.local_tessdata_dir()
    model = os.path.join(folder, "guj.traineddata")
    candidates = [os.path.join(config.PACKAGE_DIR, "tessdata"),
                  os.path.join(config.PROJECT_ROOT, "tessdata")]
    present = [c for c in candidates
               if os.path.isfile(os.path.join(c, "guj.traineddata"))]
    if present:
        assert os.path.isfile(model), \
            f"model exists in {present} but lookup returned {folder}"
        assert os.path.normcase(folder) in [os.path.normcase(p)
                                            for p in present], folder
        print(f"ok  tessdata resolved to the copy that exists ({folder})")
    else:
        assert folder in candidates or os.path.isdir(folder), folder
        print("ok  no model installed; lookup points somewhere sane")


def test_startup_never_fails_silently():
    """A double-clicked script that raises prints into a console that closes
    instantly - indistinguishable from 'nothing happened'.  A startup crash
    must reach the user: traceback, a dialog, exit code 2."""
    from tkinter import messagebox

    from notice_extractor import main as entry
    from notice_extractor.ui import app as app_mod

    shown = {}
    real_box, real_run, real_stdin = (messagebox.showerror, app_mod.run,
                                      sys.stdin)
    try:
        messagebox.showerror = lambda t, m, **k: shown.update(title=t, msg=m)
        sys.stdin = None                      # not a tty: skip the pause

        def boom():
            raise ImportError("DLL load failed while importing cv2")

        app_mod.run = boom
        code = entry.main([])
        assert code == 2, f"a startup crash returned {code}, not 2"
        assert shown, "a startup crash showed the user nothing"
        assert "could not start" in shown["title"].lower(), shown["title"]
        assert "cv2" in shown["msg"], shown["msg"]
    finally:
        messagebox.showerror, app_mod.run = real_box, real_run
        sys.stdin = real_stdin
    print("ok  a startup crash reports itself instead of vanishing")


def test_doctor_reports_the_environment():
    """--doctor must run even on a broken machine - it is what you reach for
    when nothing else works."""
    import contextlib
    import io

    from notice_extractor import main as entry

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = entry.doctor()
    text = buffer.getvalue()
    assert code in (0, 1), code
    for expected in ("python", "tkinter", "opencv-python", "tesseract",
                     "gujarati OCR model", "newspaper plugins"):
        assert expected in text, f"doctor never mentioned {expected!r}"
    print("ok  doctor checks python, tk, deps, OCR and plugins")


def test_empty_hint_goes_away_when_a_notice_arrives():
    """"No notices yet - choose a newspaper..." must vanish the moment one
    lands.  The cards are PLACED, not gridded, so a leftover gridded label
    does not get pushed out of the way - it sits visible behind them.  The
    live path (add_result -> _append_card) skips _render_page, which is
    where the hint used to be dropped, so it stayed on screen all run."""
    import numpy as np

    # A PhotoImage binds to whichever root is default when it is created, and
    # other tests here build their own roots, so this one is made default for
    # the duration - and handed back afterwards, or the tests that run later
    # lose the root they expect.
    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.withdraw()
    try:
        gallery = pne.GalleryPanel(root, on_open=lambda r: None,
                                   on_save=lambda r: None,
                                   on_click=lambda r: None)
        gallery.pack()
        root.update()
        assert gallery._empty_label.winfo_manager(), \
            "the hint should be shown on an empty gallery"

        gallery.add_heading("Sandesh  -  ahmedabad  -  11-08-2026")
        gallery.add_result(pne.NoticeResult(
            result_id=1, page_number=2, index_on_page=1,
            image_bgr=np.full((90, 70, 3), 240, np.uint8),
            confidence=95, method="box+template", edition="ahmedabad",
            newspaper="Sandesh", issue_date="2026-08-11"))
        root.update()
        assert not gallery._empty_label.winfo_manager(), \
            "the empty-gallery hint is still on screen behind the notices"
        assert gallery.cards, "no card was created"
    finally:
        root.destroy()
        tk._default_root = previous_root
    print("ok  empty-gallery hint clears as soon as a notice arrives")


def test_empty_hint_is_actually_visible():
    """The hint must be READABLE, not merely gridded.

    winfo_manager() (the check above) says a geometry manager owns the
    widget; it says nothing about whether you can see it.  The masonry pass
    sizes the canvas window from the PLACED cards, so an empty gallery came
    to gap+gap = 20 px and clipped the 109 px hint to a sliver.  The gallery
    looked broken instead of empty, and every assertion in the codebase
    still passed."""
    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.geometry("900x600")
    root.update()
    try:
        gallery = pne.GalleryPanel(root, on_open=lambda r: None,
                                   on_save=lambda r: None,
                                   on_click=lambda r: None)
        gallery.pack(fill="both", expand=True)
        for _ in range(40):
            root.update()
            time.sleep(0.005)
        needed = gallery._empty_label.winfo_reqheight()
        inner = gallery._inner.winfo_height()
        assert gallery._empty_label.winfo_ismapped(), "hint is not mapped"
        assert inner >= needed, \
            f"hint is clipped: {inner}px of canvas for a {needed}px label"
    finally:
        root.destroy()
        tk._default_root = previous_root
    print(f"ok  empty-gallery hint is visible ({inner}px for a {needed}px "
          "label)")


def test_search_bar_hides_remove_until_there_is_something_to_remove():
    """The default search UI is the word "Search" and somewhere to type.

    Clear appears only once there is a query to clear, and the dropdown's
    Clear-search-history row only exists while a history does.  A Remove
    control with nothing to remove is clutter that also lies about state."""
    from notice_extractor.utils import search as store

    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.withdraw()
    # The tests get their OWN history file.  Pointing them at the real
    # data/recent_searches.json meant two of my own processes (this suite and
    # qa_run.py) could clear and restore it at the same time - which produced
    # exactly one phantom failure and sent me looking for a bug in the code.
    saved_name = store.RECENT_FILENAME
    store.RECENT_FILENAME = "test-recent-searches.json"
    try:
        store.clear_recent()
        gallery = pne.GalleryPanel(root, on_open=lambda r: None,
                                   on_save=lambda r: None,
                                   on_click=lambda r: None)
        bar = gallery.build_search_bar(root)
        bar.pack()
        root.update()

        # winfo_manager(), not winfo_ismapped(): this root is withdrawn (and
        # a real one can be minimised), which makes every child report "not
        # mapped" whether or not it is packed.
        assert not gallery._clear_btn.winfo_manager(), \
            "Clear is on screen with nothing to clear"
        assert list(gallery._search_combo.cget("values")) == [], \
            "an empty history must not offer a Clear-history row"

        gallery.search_var.set("public notice")
        root.update()
        assert gallery._clear_btn.winfo_manager(), \
            "Clear stayed hidden even though there is a query"

        gallery.search_var.set("")
        root.update()
        assert not gallery._clear_btn.winfo_manager(), \
            "Clear stayed on screen after the query went"

        # Minimise / restore must not desync it: while a window is iconified
        # every child reads as unmapped, which is exactly the state an
        # ismapped()-based check gets wrong.
        gallery.search_var.set("land notice")
        root.update()
        root.deiconify()
        root.iconify()
        root.update()
        gallery.search_var.set("")
        root.update()
        root.deiconify()
        root.update()
        assert not gallery._clear_btn.winfo_manager(), \
            "Clear survived a minimise/restore with an empty query"
    finally:
        store.clear_recent()
        store.RECENT_FILENAME = saved_name
        root.destroy()
        tk._default_root = previous_root
    print("ok  search bar hides Clear until there is a query")


def test_recent_search_history_round_trips_through_the_ui():
    """Running a search records it; picking it back out RUNS it again.

    The point of the list is not retyping a query you already ran, so a
    dropdown that only fills the box would leave the manual step in place."""
    from notice_extractor.utils import search as store

    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.withdraw()
    # The tests get their OWN history file.  Pointing them at the real
    # data/recent_searches.json meant two of my own processes (this suite and
    # qa_run.py) could clear and restore it at the same time - which produced
    # exactly one phantom failure and sent me looking for a bug in the code.
    saved_name = store.RECENT_FILENAME
    store.RECENT_FILENAME = "test-recent-searches.json"
    try:
        store.clear_recent()
        gallery = pne.GalleryPanel(root, on_open=lambda r: None,
                                   on_save=lambda r: None,
                                   on_click=lambda r: None)
        gallery.build_search_bar(root).pack()
        fired = []

        def stub_search(query):
            """What the Application really does: run it, then report back.

            Reporting back matters - the bar refuses a second search while
            one is in flight (the button's disabled state IS that flag), so
            a stub that never finishes would wedge it after one query."""
            fired.append(query)
            gallery.search_finished(query, 0, 0)

        gallery.on_search = stub_search
        root.update()

        for query in ("public notice", "જાહેર ચેતવણી", "property notice"):
            gallery.search_var.set(query)
            gallery._fire_search()
        root.update()
        assert fired == ["public notice", "જાહેર ચેતવણી",
                         "property notice"], fired
        # Newest first.
        assert gallery.recent_searches() == [
            "property notice", "જાહેર ચેતવણી", "public notice"], \
            gallery.recent_searches()

        # Same query, different case and spacing: one row, moved to the top.
        gallery.search_var.set("PUBLIC   NOTICE")
        gallery._fire_search()
        root.update()
        assert gallery.recent_searches() == [
            "PUBLIC NOTICE", "property notice", "જાહેર ચેતવણી"], \
            gallery.recent_searches()

        # Picking a row applies it - no retyping, no second click.
        fired.clear()
        gallery.search_var.set("જાહેર ચેતવણી")
        gallery._on_history_pick()
        root.update()
        assert fired == ["જાહેર ચેતવણી"], fired

        # Removing one entry leaves the rest alone.
        gallery._forget("property notice")
        root.update()
        assert "property notice" not in gallery.recent_searches()
        assert len(gallery.recent_searches()) == 2

        # The Clear-history row is an action, never a query.
        fired.clear()
        gallery.search_var.set(gallery.CLEAR_HISTORY_ROW)
        gallery._on_history_pick()
        root.update()
        assert fired == [], "the Clear-history row was searched for"
        assert gallery.recent_searches() == []
        assert gallery.search_var.get() == "", \
            "the action row's own label was left in the search box"
    finally:
        store.clear_recent()
        store.RECENT_FILENAME = saved_name
        root.destroy()
        tk._default_root = previous_root
    print("ok  recent searches: saved, deduped, re-applied on pick")


def test_a_match_without_boxes_is_still_shown():
    """A notice can match with NO word boxes - OCR glues the phrase into one
    blob whose pieces no longer line up with the query - and search.py both
    documents and tests that case.

    The gallery used to read the verdict off `match_boxes`, so those notices
    were counted in "3 of 40 contain X" and then hidden by "Show only
    matches" and skipped by the jump-to-first-hit.  Counted but invisible."""
    import numpy as np

    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.withdraw()
    try:
        gallery = pne.GalleryPanel(root, on_open=lambda r: None,
                                   on_save=lambda r: None,
                                   on_click=lambda r: None)
        gallery.build_search_bar(root)
        gallery.pack()
        gallery.add_heading("Sandesh  -  ahmedabad  -  12-08-2026")
        glued = pne.NoticeResult(
            result_id=1, page_number=4, index_on_page=1,
            image_bgr=np.full((90, 70, 3), 240, np.uint8),
            confidence=91, method="box+template", edition="ahmedabad",
            newspaper="Sandesh", issue_date="2026-08-12",
            section_title="Sandesh  -  ahmedabad  -  12-08-2026")
        gallery.add_result(glued)
        root.update()

        # Exactly what start_search() writes for a boxless hit.
        glued.matched = True
        glued.match_boxes = []
        glued.match_query_text = "જાહેર ચેતવણી"
        gallery.search_var.set("જાહેર ચેતવણી")
        gallery._refresh_after_search()
        root.update()

        assert gallery._visible_results(0) == [glued], \
            "the search filter hid a notice the counter had just counted"
        assert gallery.cards, "the matching notice has no card"
        card = gallery.cards[0]
        labels = [w.cget("text") for w in card.winfo_children()
                  if w.winfo_class() == "TLabel"]
        assert any("Matched:" in text and "જાહેર ચેતવણી" in text
                   for text in labels), labels
    finally:
        root.destroy()
        tk._default_root = previous_root
    print("ok  a boxless match stays visible and says what it matched")


def test_searching_filters_the_gallery_by_itself():
    """A search narrows the gallery on its own.

    There is no "Show only matches" box any more: asking for something and
    then being shown everything, with the answer highlighted somewhere among
    it, is not a search result.  Clear is the way back."""
    import numpy as np

    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.withdraw()
    try:
        gallery = pne.GalleryPanel(root, on_open=lambda r: None,
                                   on_save=lambda r: None,
                                   on_click=lambda r: None)
        gallery.build_search_bar(root)
        gallery.pack()
        gallery.add_heading("Sandesh  -  ahmedabad  -  12-08-2026")
        made = []
        for index in range(4):
            result = pne.NoticeResult(
                result_id=index + 1, page_number=index + 1, index_on_page=1,
                image_bgr=np.full((80, 60, 3), 240, np.uint8),
                confidence=90, method="box+template", edition="ahmedabad",
                newspaper="Sandesh", issue_date="2026-08-12",
                section_title="Sandesh  -  ahmedabad  -  12-08-2026")
            gallery.add_result(result)
            made.append(result)
        root.update()
        assert len(gallery._visible_results(0)) == 4

        # Two of them match - no checkbox is touched anywhere below.
        for result in made[:2]:
            result.matched = True
            result.match_query_text = "જાહેર ચેતવણી"
        gallery.search_var.set("જાહેર ચેતવણી")
        gallery.search_finished("જાહેર ચેતવણી", 2, 4)
        root.update()
        assert gallery._visible_results(0) == made[:2], \
            "the gallery still shows notices that did not match"
        assert len(gallery.cards) == 2, f"{len(gallery.cards)} cards drawn"

        # Nothing matches -> say so, and say how to get back.
        for result in made:
            result.matched = False
        gallery.search_var.set("zebra")
        gallery.search_finished("zebra", 0, 4)
        root.update()
        assert not gallery.cards, "cards drawn for a search with no matches"
        hint = gallery._empty_label.cget("text")
        assert "zebra" in hint and "Clear" in hint, hint
        assert gallery._empty_label.winfo_manager(), "no empty state shown"

        # Clear brings everything back.
        gallery.clear_search()
        root.update()
        assert len(gallery._visible_results(0)) == 4, "Clear did not restore"
    finally:
        root.destroy()
        tk._default_root = previous_root
    print("ok  a search filters the gallery on its own, Clear restores it")


def test_notice_type_click_filters_what_is_on_screen():
    """Clicking a Notice-type button filters the notices already found.

    It used to only change what the NEXT run would extract, so with a full
    gallery on screen the click did nothing visible - which is the same
    complaint that turned the dropdown into buttons in the first place.

    The filter is STRICT: an unidentified notice does not match a specific
    type.  Letting unknowns through was the first design, and on real
    newsprint 29% of crops came back unknown - so "show me જાહેર ચેતવણી"
    answered with a screen of જાહેર નોટિસ.  They are counted and reported
    instead, and "All" still shows everything."""
    import numpy as np

    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.withdraw()
    was = pne.active_notice_type()
    try:
        gallery = pne.GalleryPanel(root, on_open=lambda r: None,
                                   on_save=lambda r: None,
                                   on_click=lambda r: None)
        gallery.build_search_bar(root)
        gallery.pack()
        gallery.add_heading("Sandesh  -  ahmedabad  -  12-08-2026")
        kinds = ["notice", "notice", "chetavni", ""]
        made = []
        for index, kind in enumerate(kinds):
            result = pne.NoticeResult(
                result_id=index + 1, page_number=index + 1, index_on_page=1,
                image_bgr=np.full((80, 60, 3), 240, np.uint8),
                confidence=90, method="box+template", edition="ahmedabad",
                newspaper="Sandesh", issue_date="2026-08-12",
                section_title="Sandesh  -  ahmedabad  -  12-08-2026")
            result.notice_type = kind
            result.ocr_done = True
            gallery.add_result(result)
            made.append(result)
        root.update()

        pne.set_notice_type("All")
        gallery.apply_type_filter()
        root.update()
        assert len(gallery._visible_results(0)) == 4, "All must show all 4"

        pne.set_notice_type("જાહેર ચેતવણી")
        gallery.apply_type_filter()
        root.update()
        shown = gallery._visible_results(0)
        assert shown == [made[2]], \
            f"ચેતવણી filter should show only the ચેતવણી notice, got {shown}"
        assert made[3] not in shown, \
            "an unidentified notice matched a specific type filter"
        assert len(gallery.cards) == 1, f"{len(gallery.cards)} cards drawn"
        assert gallery.unidentified_count() == 1, \
            "the unidentified notice must be counted, not just dropped"

        pne.set_notice_type("જાહેર નોટિસ")
        gallery.apply_type_filter()
        root.update()
        shown = gallery._visible_results(0)
        assert shown == made[:2], f"નોટિસ filter showed {shown}"
        assert made[2] not in shown, "a ચેતવણી survived the નોટિસ filter"
        assert made[3] not in shown, "an unidentified notice slipped through"

        pne.set_notice_type("All")
        gallery.apply_type_filter()
        root.update()
        assert len(gallery._visible_results(0)) == 4, "All did not restore"
    finally:
        pne.set_notice_type({"notice": "જાહેર નોટિસ",
                             "chetavni": "જાહેર ચેતવણી"}.get(was, "All"))
        root.destroy()
        tk._default_root = previous_root
    print("ok  notice-type buttons filter the gallery on click")


def test_saving_and_counting_follow_the_filter():
    """"Save All" must mean the notices you are looking at.

    Filtering to જાહેર ચેતવણી, pressing Save All and getting back the
    notices you just filtered out is the same class of bug as counting a
    match and then refusing to show it."""
    import numpy as np

    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.withdraw()
    was = pne.active_notice_type()
    try:
        gallery = pne.GalleryPanel(root, on_open=lambda r: None,
                                   on_save=lambda r: None,
                                   on_click=lambda r: None)
        gallery.build_search_bar(root)
        gallery.pack()
        gallery.add_heading("Sandesh  -  ahmedabad  -  12-08-2026")
        made = []
        for index, kind in enumerate(["notice", "notice", "chetavni"]):
            result = pne.NoticeResult(
                result_id=index + 1, page_number=index + 1, index_on_page=1,
                image_bgr=np.full((80, 60, 3), 240, np.uint8),
                confidence=90, method="box+template", edition="ahmedabad",
                newspaper="Sandesh", issue_date="2026-08-12",
                section_title="Sandesh  -  ahmedabad  -  12-08-2026")
            result.notice_type = kind
            result.ocr_done = True
            gallery.add_result(result)
            made.append(result)
        root.update()

        pne.set_notice_type("All")
        gallery.apply_type_filter()
        root.update()
        assert not gallery.is_filtered()
        assert len(gallery.visible_results()) == 3
        assert len(gallery.selected_results()) == 3

        pne.set_notice_type("જાહેર ચેતવણી")
        gallery.apply_type_filter()
        root.update()
        assert gallery.is_filtered()
        assert gallery.visible_results() == [made[2]], \
            "Save All would have written the filtered-out notices"
        assert gallery.selected_results() == [made[2]]
        # ...while the run's full set is still reachable, or a search could
        # never find anything the type buttons are currently hiding.
        assert len(gallery.all_results()) == 3

        # A search narrows on top of the type filter, not instead of it.
        made[2].matched = False
        made[0].matched = True
        gallery.search_var.set("public notice")
        gallery._refresh_after_search()
        root.update()
        assert gallery.visible_results() == [], \
            "the two filters must compose, not replace each other"
    finally:
        pne.set_notice_type({"notice": "જાહેર નોટિસ",
                             "chetavni": "જાહેર ચેતવણી"}.get(was, "All"))
        root.destroy()
        tk._default_root = previous_root
    print("ok  saving and counting follow the filter, and filters compose")


def test_a_late_log_line_cannot_undo_the_shutdown_wipe():
    """Quitting deletes data/logs.  A thread still winding down must not put
    it straight back.

    log() calls log_path(), and log_path() recreates the folder on demand -
    so a browser session or a cancelled agent writing one more line after
    clear_run_data() resurrected exactly what shutdown had just removed.
    Caught by a live QA run that took 249 s: on a slow, loaded machine the
    teardown threads were still going when the window closed."""
    import shutil
    import tempfile

    from notice_extractor import config
    from notice_extractor.utils import logger as run_logger

    # A private log folder, NOT data/logs.  The real one can legitimately be
    # held open by a second copy of the app (config.clear_run_data() says so
    # itself), and this test is about the reopen race, not about whether
    # Windows will let us win a fight with another process.
    real_dir = config.LOG_DIR
    temp_dir = tempfile.mkdtemp(prefix="pne-logtest-")
    try:
        config.LOG_DIR = temp_dir
        run_logger.reopen()
        run_logger.close()
        run_logger.log("before the quit", "info")
        run_logger.flush()
        assert os.listdir(temp_dir), "the log file was never written"

        # What Application._on_close() does, in order.
        run_logger.close(final=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
        assert not os.path.isdir(temp_dir), "the log folder did not go"

        # ...and now the straggler: a browser session or a cancelled agent
        # writing one more line after the wipe.
        run_logger.log("a browser session winding down", "dim")
        run_logger.log("a cancelled agent", "warn")
        assert not os.path.isdir(temp_dir), \
            "a late log line recreated the log folder after shutdown wiped it"

        # Logging comes back for the next run, not stuck off forever.
        run_logger.reopen()
        run_logger.log("a new run", "info")
        assert os.path.isdir(temp_dir), "logging never recovered"
    finally:
        run_logger.reopen()
        run_logger.close()
        config.LOG_DIR = real_dir
        shutil.rmtree(temp_dir, ignore_errors=True)
    print("ok  a late log line cannot undo the shutdown wipe")


def test_a_new_run_unsticks_a_background_read():
    """Starting a run must not wedge search and the notice-type filter.

    _begin_run swaps in a FRESH message queue so a cancelled worker's late
    messages cannot reach the UI.  A background crop read (Find-text, or a
    Notice-type click) captured the OLD queue when it started, so the "done"
    message that clears `_searching` lands somewhere nothing drains.  Left
    set, that flag makes start_search return early FOREVER: every later
    search and every notice-type click is silently refused, and the Search
    button - disabled awaiting a reply that can never arrive - stays dead.

    This is what hung a live QA run for nine minutes with no error."""
    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.withdraw()
    try:
        app = pne.Application(root)
        root.update()

        # Exactly the state a background read leaves behind.
        orphaned = app._msg_queue
        app._searching = True
        app.gallery._search_btn.configure(state="disabled")
        app.gallery.search_var.set("જાહેર ચેતવણી")
        root.update()

        app._begin_run()
        root.update()
        assert app._msg_queue is not orphaned, "the queue was not replaced"
        assert not app._searching, \
            "_searching survived the queue swap - every later search is dead"
        assert "disabled" not in app.gallery._search_btn.state(), \
            "the Search button stayed disabled awaiting a lost reply"
        assert app.gallery.search_var.get() == "", \
            "a stale query would hide the whole new run behind 'no match'"

        # And the app is genuinely usable again.
        fired = []
        app.gallery.on_search = lambda q: (fired.append(q),
                                           app.gallery.search_finished(q, 0, 0))
        app.gallery.search_var.set("public notice")
        app.gallery._fire_search()
        root.update()
        assert fired == ["public notice"], fired
    finally:
        app._running = False
        root.destroy()
        tk._default_root = previous_root
    print("ok  a new run un-sticks a background read (search stays alive)")


def _feedback_result(pne, text="", confidence=70, review=False, rid=1):
    import numpy as np
    result = pne.NoticeResult(
        result_id=rid, page_number=4, index_on_page=1,
        image_bgr=np.full((80, 60, 3), 240, np.uint8),
        confidence=confidence, method="box+template", edition="ahmedabad",
        newspaper="Gujarat Samachar", issue_date="2026-08-12",
        section_title="Gujarat Samachar  -  ahmedabad  -  12-08-2026")
    result.ocr_text = text
    result.normalized_ocr = pne.normalize_ocr_text(text)
    result.ocr_done = bool(text)
    result.notice_type = "notice"
    result.needs_review = review
    return result


def test_one_bad_message_does_not_kill_the_pump():
    """_poll_queue is the only bridge from the workers to the window.

    It used to reschedule itself on the last line of the happy path, so a
    single exception out of a handler skipped the reschedule and killed it
    permanently: the window stayed up, the run kept going, and nothing it
    produced ever reached the screen again.  `_searching` stuck true forever
    because the message that clears it could no longer be delivered, and the
    only visible symptom was a search timing out after two minutes."""
    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.withdraw()
    try:
        app = pne.Application(root)
        root.update()

        seen = []
        original = app._handle_message

        def explode(message):
            if message[0] == "boom":
                raise RuntimeError("handler blew up")
            seen.append(message[0])
            return original(message)

        app._handle_message = explode
        app._msg_queue.put(("boom",))
        app._msg_queue.put(("log", "after the explosion", "info"))
        for _ in range(40):
            root.update()
            time.sleep(0.01)

        assert "log" in seen, \
            "the pump died on a bad message - nothing after it was delivered"

        # ...and it is still alive several messages later.
        app._msg_queue.put(("log", "still pumping", "info"))
        for _ in range(30):
            root.update()
            time.sleep(0.01)
        assert seen.count("log") >= 2, seen
    finally:
        app._running = False
        root.destroy()
        tk._default_root = previous_root
    print("ok  one bad message is logged and skipped; the pump survives")


def test_normal_cards_ask_one_question_only():
    """A result card offers "Not Related" and NOTHING else.

    The notice is in the results because the app already believes it
    belongs, so the only useful question left is whether it got that wrong.
    "This Is Right" on every card would make the common case - the app was
    right, say nothing - into a decision on every notice.  That button
    exists in exactly one place, the Not Sure queue, where the app genuinely
    does not know."""
    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.withdraw()
    try:
        seen = []
        card = pne.NoticeCard(root, _feedback_result(pne),
                              on_open=lambda r: None, on_save=lambda r: None,
                              on_click=lambda r: None,
                              on_copy=lambda r: seen.append("copy"),
                              on_feedback=lambda r, v: seen.append(v))
        root.update()

        labels = []
        def walk(w):
            for child in w.winfo_children():
                try:
                    text = str(child.cget("text"))
                except Exception:
                    text = ""
                if child.winfo_class() in ("TButton", "Button") and text:
                    labels.append(text)
                walk(child)
        walk(card)

        assert any("Not Related" in t for t in labels), labels
        for banned in ("This Is Right", "Not Sure", "I Need This",
                       "I Don't Need This", "Maybe"):
            assert not any(banned in t for t in labels), \
                f"{banned!r} must not appear on a normal result card: {labels}"
        for wanted in ("Open", "Save", "Copy"):
            assert any(t == wanted for t in labels), \
                f"the {wanted} button is missing: {labels}"

        # The buttons actually call back.
        for label in labels:
            if label == "Copy" or "Not Related" in label:
                for child in _all_children(card):
                    try:
                        if str(child.cget("text")) == label:
                            child.invoke()
                            break
                    except Exception:
                        continue
        assert "copy" in seen and "negative" in seen, seen
    finally:
        root.destroy()
        tk._default_root = previous_root
    print(f"ok  normal card offers {sorted(set(labels))} - no confirm button")


def _all_children(widget):
    out = []
    for child in widget.winfo_children():
        out.append(child)
        out.extend(_all_children(child))
    return out


def test_review_queue_is_the_only_place_you_confirm():
    """The Not Sure queue carries BOTH buttons, and records both verdicts."""
    import tempfile
    from notice_extractor import config
    from notice_extractor.utils import feedback as fb

    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.withdraw()
    folder = tempfile.mkdtemp(prefix="pne-review-")
    original = config.DATA_DIR
    config.DATA_DIR = folder
    try:
        verdicts = []
        queue = [_feedback_result(pne, "advertisement circus tickets",
                                  review=True, rid=1),
                 _feedback_result(pne, "આથી જાહેર જનતાને જણાવવાનું",
                                  review=True, rid=2)]

        def verdict(r, v):
            # What Application.on_feedback does to the result itself.
            verdicts.append((r.result_id, v))
            r.rejected = (v == "negative")

        dialog = pne.ReviewDialog(root, queue, verdict, "Segoe UI")
        root.update()
        labels = [str(c.cget("text")) for c in _all_children(dialog)
                  if c.winfo_class() in ("TButton", "Button")]
        assert any("This Is Right" in t for t in labels), labels
        assert any("Not Related" in t for t in labels), labels
        assert any("Previous" in t for t in labels), labels
        assert any("Next" in t for t in labels), labels

        dialog._decide("negative")           # #1 dismissed - but KEPT
        root.update()
        assert dialog._current() is queue[1]     # moved on to #2
        dialog._decide("positive")           # #2 confirmed - leaves
        root.update()
        assert verdicts == [(1, "negative"), (2, "positive")], verdicts
        # The dismissed one is still here, restorable: Not Related is off,
        # This Is Right is on.
        assert dialog._current() is queue[0]
        assert "disabled" in dialog._wrong_btn.state()
        assert "disabled" not in dialog._right_btn.state()
        dialog._decide("positive")           # restored
        root.update()
        assert dialog._current() is None
        assert "disabled" in dialog._right_btn.state()
        dialog.destroy()
    finally:
        config.DATA_DIR = original
        root.destroy()
        tk._default_root = previous_root
        shutil.rmtree(folder, ignore_errors=True)
    print("ok  review queue confirms and rejects; both are recorded")


def test_learning_needs_repetition_and_protects_recall():
    """Feedback has to change future runs - without eating real notices."""
    import tempfile
    from notice_extractor import config
    from notice_extractor.utils import feedback as fb

    folder = tempfile.mkdtemp(prefix="pne-learn-")
    original = config.DATA_DIR
    config.DATA_DIR = folder
    try:
        advert = "ASIAD CIRCUS tickets available bookmyshow evening show"
        notice = "આથી જાહેર જનતાને જણાવવાનું કે સદરહુ મિલકત અંગે હક્ક હિસ્સો"

        one = _feedback_result(pne, advert, confidence=70)
        fb.record(one, "negative")
        fb.relearn()
        assert not fb.load_model()["weights"], "one click became a rule"

        for i in range(fb.MIN_SUPPORT):
            fb.record(_feedback_result(pne, advert + f" hall {i}"), "negative")
        fb.relearn()
        model = fb.load_model()
        assert model["weights"], "repetition taught nothing"

        # Generalises to a DIFFERENT advert sharing wording...
        similar = _feedback_result(pne, "ASIAD CIRCUS tickets on bookmyshow",
                                   confidence=70)
        assert fb.should_demote(similar, model), "learned nothing reusable"
        # ...but a real notice is untouched.
        real = _feedback_result(pne, notice, confidence=70)
        assert not fb.should_demote(real, model), "a real notice was demoted"
        # ...and a confident detection is never demoted, whatever it says.
        loud = _feedback_result(pne, advert + " hall 1", confidence=95)
        assert not fb.should_demote(loud, model), \
            "learning overruled a confident template match"

        # apply_learning only ever sets `demoted`, never `rejected`.
        results = [similar, real, loud]
        pne.apply_learning(results)
        assert similar.demoted and not similar.rejected
        assert not real.demoted and not loud.demoted

        # Versioned, and revertible.
        before = fb.load_model()["version"]
        fb.relearn()
        assert fb.load_model()["version"] == before + 1
        assert fb.rollback() is not None
        positive, negative = fb.counts()
        assert (positive, negative) == (0, fb.MIN_SUPPORT + 1)
    finally:
        config.DATA_DIR = original
        shutil.rmtree(folder, ignore_errors=True)
    print("ok  learning needs repetition, generalises, and cannot eat a "
          "confident notice")


def test_short_negative_keywords_do_not_veto_on_a_name():
    """A 4-letter veto keyword must not match a person's name.

    NEGATIVE_FUZZY_RATIO is 0.84.  On a 4-character word that is 3 characters
    right and the fourth free - so "ભરતી" (recruitment) matched at 0.857
    inside "ઘરતીબેન", a woman's name, and vetoed a real City Civil Court
    notice on page 15 of a real edition.  The notice cleared every other
    test in the pipeline and was thrown away on a name.

    utils.search has had the same rule for query tokens since it was written
    (FUZZY_MIN_TOKEN_LEN); the keyword matchers never got it."""
    # The exact text that lost a notice.
    lost = "આંક-૦૭ અરજદાર - દેસાઈ ઘરતીબેન કીરીટભાઈ તે ભાવીનભાઈ અશ્વિનભાઈ"
    ratio, keyword = pne.match_negative_text(lost)
    assert ratio == 0.0, \
        f"a name still trips the veto: {keyword!r} at {ratio:.3f}"

    # Every short keyword must be exact-only, so no near-miss can veto.
    short = [k for k in pne.NEGATIVE_KEYWORDS
             if len(pne.normalize_ocr_text(k)) < pne.FUZZY_MIN_KEYWORD_LEN]
    assert short, "no short negative keywords - has the list changed?"
    for keyword in short:
        near = pne.normalize_ocr_text(keyword)
        # swap the first character: one wrong letter out of four
        mutated = ("ઘ" if near[0] != "ઘ" else "ખ") + near[1:]
        assert pne.match_negative_text(mutated)[0] == 0.0, \
            f"{keyword!r} still fuzzy-matches {mutated!r}"

    # ...while the real thing is still vetoed, which is the whole point.
    for text, expect in (
            ("ભરતી જાહેરાત માટે અરજી મંગાવવામાં આવે છે", True),
            ("ઈ-ટેન્ડર નોટિસ પ્રસિદ્ધ કરવામાં આવે છે", True),
            ("જાહેર હરાજી થી વેચાણ કરવાનું છે", True),
            ("કબજા નોટિસ સ્થાવર મિલકત", True),
            ("આથી જાહેર જનતાને જણાવવાનું કે", False)):
        got = pne.match_negative_text(text)[0] > 0
        assert got is expect, f"{text[:30]!r} -> {got}, wanted {expect}"
    print(f"ok  short negative keywords are exact-only "
          f"({len(short)} of them), real vetoes still fire")


def test_status_log_starts_closed_and_comes_back_whole():
    """The log is a diagnostic, not the point of the screen.

    It opened by default and took a third of the window from the notices.
    Closed now - but it must keep recording while hidden and come back at a
    real width, which is the bug ui/app.py was written to fix."""
    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.geometry("1300x800")
    root.update()
    try:
        app = pne.Application(root)
        for _ in range(60):
            root.update()
            time.sleep(0.005)

        assert not app._log_visible, "the log opened by default again"
        assert not app.log_panel.winfo_ismapped(), "the log pane is on screen"
        assert app._show_log_btn.winfo_manager(), \
            "no way back in - the ☰ Log button is not shown"

        # It records while hidden, or closing it would lose the run's log.
        app.log_panel.log("recorded while the panel was closed", "info")
        for _ in range(30):
            root.update()
            time.sleep(0.005)

        app.toggle_log()
        for _ in range(60):
            root.update()
            time.sleep(0.005)
        assert app._log_visible and app.log_panel.winfo_ismapped()
        assert app.log_panel.winfo_width() >= pne.LOG_PANE_MIN_WIDTH, \
            f"reopened at {app.log_panel.winfo_width()}px"
        shown = app.log_panel._text.get("1.0", "end")
        assert "recorded while the panel was closed" in shown, \
            "lines logged while hidden were lost"

        app.toggle_log()
        for _ in range(30):
            root.update()
            time.sleep(0.005)
        assert not app._log_visible
        assert app._show_log_btn.winfo_manager(), "the ☰ Log button vanished"
    finally:
        app._running = False
        root.destroy()
        tk._default_root = previous_root
    print("ok  status log starts closed, keeps recording, reopens whole")


def test_help_guide_opens_without_breaking_scrolling():
    """Help > How to use this app is a real guide, and must not cost the
    app its mouse wheel.

    The obvious way to scroll a dialog is bind_all("<MouseWheel>") - which
    REPLACES the Application's own global wheel handler, and unbinding it on
    close then leaves the gallery unscrollable."""
    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.geometry("1200x760")
    root.update()
    try:
        app = pne.Application(root)
        root.update()
        before = root.bind_all("<MouseWheel>")
        assert before, "the app never installed its global wheel handler"

        app._show_help()
        for _ in range(40):
            root.update()
            time.sleep(0.005)
        dialogs = [w for w in root.winfo_children()
                   if isinstance(w, pne.HelpDialog)]
        assert dialogs, "Help > How to use this app opened nothing"
        dialog = dialogs[0]

        # It has to actually say something, in order.
        assert len(pne.HELP_STEPS) >= 6, "the guide is a stub"
        steps = sum(len(s) for _h, s in pne.HELP_STEPS)
        assert steps >= 20, f"only {steps} steps"
        headings = [h for h, _s in pne.HELP_STEPS]
        assert headings[0].startswith("1."), headings[0]
        # The things a first-time user has to be told.
        joined = " ".join(h + " " + " ".join(s) for h, s in pne.HELP_STEPS)
        for word in ("Extract", "Save", "Search", "જાહેર ચેતવણી",
                     "Downloads & Setup", "Cancel"):
            assert word in joined, f"the guide never mentions {word!r}"

        dialog.destroy()
        root.update()
        assert root.bind_all("<MouseWheel>") == before, \
            "closing Help took the app's global wheel binding with it"
    finally:
        app._running = False
        root.destroy()
        tk._default_root = previous_root
    print(f"ok  help guide: {len(pne.HELP_STEPS)} sections, {steps} steps, "
          "wheel binding intact")


def test_the_ocr_header_test_reads_a_shallower_band():
    """The text test for "is there a header here?" must look at a shallower
    band than template matching does.

    At 32% of the box it read a third of the way down, and Gujarati notices
    close with "આ જાહેર નોટીસથી જાહેર જનતાને નોંધ લેવા વિનંતી" - so a
    continuation column with no header of its own matched on its own body
    text and was cropped as a notice.  Measured over 46 real crops: genuine
    headers sit at 1-3% of the box, the false matches at 25% and 27%."""
    import numpy as np

    cfg = pne.DetectionConfig()
    assert cfg.ocr_header_frac < cfg.strip_frac_of_box, \
        "the OCR band is not shallower than the template band"
    assert cfg.ocr_header_frac <= 0.22, \
        f"{cfg.ocr_header_frac} still reaches the body text at 25%"
    assert cfg.ocr_header_frac >= 0.10, \
        f"{cfg.ocr_header_frac} is tighter than the headers it must contain"

    class _Pipeline(pne.NoticeDetectionPipeline):
        def __init__(self):
            self._cfg = cfg

    page = np.zeros((900, 700), np.uint8)
    box = (50, 100, 300, 500)            # a 500px-tall notice
    wide = _Pipeline()._header_strip(page, box)
    narrow = _Pipeline()._header_strip(page, box, cfg.ocr_header_frac)
    assert narrow.shape[0] < wide.shape[0], \
        f"the OCR band ({narrow.shape[0]}px) is not shorter than the " \
        f"template band ({wide.shape[0]}px)"
    # A header at 3% of the box is inside it; body text at 25% is not.
    assert narrow.shape[0] > 500 * 0.03, "a real header would be cut off"
    assert narrow.shape[0] < 500 * 0.25, "body text is still included"
    print(f"ok  OCR header band is {narrow.shape[0]}px of a 500px box "
          f"(template band {wide.shape[0]}px)")


def test_the_preview_drops_a_notice_the_filter_hid():
    """Filtering must not leave the preview showing what it just hid.

    The gallery emptied correctly when you clicked જાહેર ચેતવણી - and the
    biggest image on screen carried on showing a જાહેર નોટિસ, because the
    preview keeps whatever was last clicked.  The two panels disagreed, and
    the one the eye goes to was the wrong one."""
    import numpy as np

    previous_root = getattr(tk, "_default_root", None)
    root = tk.Tk()
    tk._default_root = root
    root.withdraw()
    was = pne.active_notice_type()
    try:
        app = pne.Application(root)
        root.update()
        pne.set_notice_type("All")

        made = []
        for index, kind in enumerate(("notice", "notice", "chetavni")):
            result = pne.NoticeResult(
                result_id=index + 1, page_number=index + 1, index_on_page=1,
                image_bgr=np.full((90, 70, 3), 240, np.uint8),
                confidence=90, method="box+template", edition="ahmedabad",
                newspaper="Sandesh", issue_date="2026-08-12",
                section_title="Sandesh  -  ahmedabad  -  12-08-2026")
            result.notice_type = kind
            result.ocr_done = True
            app.gallery.add_result(result)
            made.append(result)
        root.update()

        # Looking at a નોટિસ, then filtering to ચેતવણી.
        app.show_in_preview(made[0])
        root.update()
        assert app.preview._result is made[0]

        pne.set_notice_type("જાહેર ચેતવણી")
        app.gallery.apply_type_filter()
        root.update()
        assert app.preview._result is made[2], \
            "the preview kept a જાહેર નોટિસ while the filter said ચેતવણી"

        # Nothing survives the filter -> the preview empties rather than
        # lying about what is on screen.
        made[2].notice_type = "notice"
        app.gallery.apply_type_filter()
        root.update()
        assert app.gallery.visible_results() == []
        assert app.preview._result is None, "the preview outlived every notice"

        # Back to All: the preview is left alone, nothing is being hidden.
        pne.set_notice_type("All")
        app.gallery.apply_type_filter()
        root.update()
        assert len(app.gallery.visible_results()) == 3
    finally:
        pne.set_notice_type({"notice": "જાહેર નોટિસ",
                             "chetavni": "જાહેર ચેતવણી"}.get(was, "All"))
        app._running = False
        root.destroy()
        tk._default_root = previous_root
    print("ok  the preview drops a notice the filter hid")


def test_recording_the_header_family_changes_no_detection():
    """strip_score() now also records WHICH template won.  That must be a
    pure observation: the score it returns, and therefore every detection
    decision made from it, has to be bit-identical to the old loop."""
    import random

    import numpy as np

    class _Verifier(pne.HeaderTemplateVerifier):
        def __init__(self, templates):
            # No __init__: template building needs fonts, a reporter and a
            # config.  This test is only about the max-vs-argmax rewrite.
            self.templates = templates
            self._cfg = pne.DetectionConfig()
            self.last_strip_family = ""

    def old_strip_score(verifier, strip):
        """The loop this replaced."""
        if strip.size == 0 or not verifier.templates:
            return 0.0
        best = 0.0
        for _label, template in verifier.templates:
            best = max(best, verifier._match_one(
                strip, template, verifier._cfg.strip_scales))
        return best

    random.seed(23)
    rng = np.random.default_rng(23)
    templates = [(f"gs-{i}", rng.integers(0, 255, (24, 90), dtype=np.uint8))
                 for i in range(3)]
    templates += [(f"chetavni-{i}",
                   rng.integers(0, 255, (24, 90), dtype=np.uint8))
                  for i in range(2)]
    verifier = _Verifier(templates)

    checked = 0
    for _ in range(60):
        h = random.randint(30, 70)
        w = random.randint(120, 260)
        strip = rng.integers(0, 255, (h, w), dtype=np.uint8)
        new = verifier.strip_score(strip)
        old = old_strip_score(verifier, strip)
        assert new == old, f"score moved: {old} -> {new}"
        # And the family it recorded is one it could actually have matched.
        assert verifier.last_strip_family in ("notice", "chetavni", "")
        checked += 1
    assert verifier.strip_score(np.zeros((0, 0), np.uint8)) == 0.0
    print(f"ok  recording the header family changed no score ({checked} strips)")


def test_the_header_family_reaches_the_notice():
    """The type read off the header strip must survive all the way to the
    NoticeResult, including through a split box.

    This is the path that matters: 11 of 13 crops that the crop-level
    classifier could not label had a perfectly legible header - psm 11 just
    drops a big isolated title line.  Detection had already read it."""
    det = pne.Detection((10, 10, 200, 300), 0.91, "box+ocr", "chetavni")
    assert det.family == "chetavni"
    assert pne.Detection((0, 0, 1, 1), 0.5, "box+template").family == "", \
        "family must default to unknown, not to a guess"

    for keyword, expected in (
            ("જાહેર નોટિસ", "notice"),
            ("જાહેર નોટીસ", "notice"),
            ("public notice", "notice"),
            ("જાહેર ચેતવણી", "chetavni"),
            ("જાહેરચેતવણી", "chetavni"),
            ("public warning", "chetavni"),
            ("jaher chetavni", "chetavni"),
            ("", "")):
        got = pne.family_of_keyword(keyword)
        assert got == expected, f"{keyword!r} -> {got!r}, wanted {expected!r}"

    # Detection's answer wins over the crop's, and is not overwritten by it.
    import numpy as np

    def fresh(notice_type):
        r = pne.NoticeResult(
            result_id=1, page_number=3, index_on_page=1,
            image_bgr=np.full((200, 150, 3), 240, np.uint8),
            confidence=91, method="box+ocr", notice_type=notice_type)
        r.ocr_text = "આ નોટીસ પ્રસિદ્ધ થયેથી દિન-૭માં"     # body says નોટિસ
        return r

    # (a) crops already read: the engine-is-None path.
    already = fresh("chetavni")
    already.ocr_done = True
    pne.read_notice_crops([already], None)
    assert already.notice_type == "chetavni", \
        "the body text overrode the header the detector actually read"

    # (b) crops NOT yet read: the path that actually runs OCR.  This is the
    # one that overwrote a good detection answer with a worse crop-level one
    # and put 13 of 59 real notices back to "unknown" - and the None-engine
    # test above passed the whole time, because only that branch was guarded.
    class _Engine:
        """Reads the BODY only - which is what psm 11 really does to these
        crops: it drops the big display-type header line."""
        name = "stub"

        def read_words(self, gray):
            return [pne.OcrWord(text=t, x=0, y=120, w=40, h=14, conf=90)
                    for t in "આ નોટીસ પ્રસિદ્ધ થયેથી દિન-૭માં".split()]

    unread = fresh("chetavni")
    pne.read_notice_crops([unread], _Engine())
    assert unread.ocr_done, "the crop was never marked read"
    assert unread.notice_type == "chetavni", \
        "reading the crop overwrote the type detection had already read"

    # ...but a notice detection could NOT identify still gets classified.
    unknown = fresh("")
    pne.read_notice_crops([unknown], _Engine())
    assert unknown.notice_type == "notice", \
        "an unidentified notice was left unclassified"
    print("ok  the header's notice type reaches the result and is not "
          "overwritten")


def test_notice_type_is_read_off_the_crop():
    """classify_notice_text labels a crop from its own printed header, and
    does NOT change its answer because of what the run is filtering for."""
    was = pne.active_notice_type()
    try:
        for mode in pne.NOTICE_TYPE_CHOICES:
            pne.set_notice_type(mode)
            for text, expected in (
                    ("જાહેર નોટિસ આથી જણાવવામાં આવે છે", "notice"),
                    ("જાહેર ચેતવણી આથી જાહેર જનતાને", "chetavni"),
                    ("જાહેરચેતવણી મિલકત બાબત", "chetavni"),
                    ("PUBLIC NOTICE is hereby given", "notice"),
                    ("Public Warning about plot 12", "chetavni"),
                    ("સર્વે નંબર ૪૨ ની મિલકત", ""),
                    ("", "")):
                got = pne.classify_notice_text(text)
                assert got == expected, \
                    f"{text[:30]!r} -> {got!r}, wanted {expected!r} " \
                    f"(toggle was {mode})"
    finally:
        pne.set_notice_type({"notice": "જાહેર નોટિસ",
                             "chetavni": "જાહેર ચેતવણી"}.get(was, "All"))
    print("ok  notice type is read off the crop, whatever the toggle says")


def test_one_category_covers_notice_and_chetavni():
    """જાહેર નોટિસ and જાહેર ચેતવણી are ONE category, matched by one call
    against one piece of text - not two pipelines and not two OCR passes.

    Also covers the OCR damage the request lists: extra spaces, missing
    spaces, split lines, dropped matras, and the Latin spellings (header
    strips are read guj+eng, so an English header must not be a silent
    miss)."""
    was = pne.active_notice_type()
    try:
        pne.set_notice_type("All")
        for text in ("જાહેર નોટિસ આથી જણાવવામાં આવે છે",
                     "જાહેર ચેતવણી આથી જાહેર જનતાને",
                     "જાહેર  ચેતવણી",          # extra spaces
                     "જાહેરચેતવણી",             # no space at all
                     "જાહેર\nચેતવણી",           # split across lines
                     "જાહેર ચેતવણિ",            # OCR dropped the matra
                     "PUBLIC NOTICE is hereby given",
                     "JAHER CHETAVNI to all concerned",
                     "Public Warning regarding survey no 42"):
            score, keyword = pne.match_notice_text(text, False)
            assert score > 0, f"missed {text!r}"
            assert keyword in pne.STRICT_KEYWORDS, keyword

        # ...and the vetoes still veto.
        for text in ("ટેન્ડર નોટિસ", "જાહેર હરાજી", "TENDER NOTICE",
                     "public auction of the plot"):
            assert pne.match_notice_text(text, False)[0] == 0.0 or \
                pne.match_negative_text(text)[0] > 0, \
                f"{text!r} would be extracted as a public notice"

        # The toggle narrows the SAME category rather than switching engine.
        pne.set_notice_type("જાહેર નોટિસ")
        assert pne.match_notice_text("જાહેર ચેતવણી આથી", False)[0] == 0.0
        assert pne.match_notice_text("Public Warning here", False)[0] == 0.0
        pne.set_notice_type("જાહેર ચેતવણી")
        assert pne.match_notice_text("જાહેર નોટિસ આથી", False)[0] == 0.0
        assert pne.match_notice_text("PUBLIC NOTICE here", False)[0] == 0.0
        assert pne.match_notice_text("જાહેર ચેતવણી આથી", False)[0] > 0.0
    finally:
        pne.set_notice_type({"notice": "જાહેર નોટિસ",
                             "chetavni": "જાહેર ચેતવણી"}.get(was, "All"))
    print(f"ok  one category ({pne.NOTICE_CATEGORY}) covers નોટિસ + ચેતવણી "
          f"in {len(pne.STRICT_KEYWORDS)} spellings")


def test_keyword_script_guard_changes_no_result():
    """Carrying the English spellings must not cost anything on a Gujarati
    page, and the shortcut that makes it free must be exact.

    match_notice_text skips keywords written in a script the text does not
    contain at all.  That is sound because fuzzy_contains compares
    characters - a Latin needle against a haystack with no Latin scores 0 -
    but "sound" is worth an assertion, because a wrong skip is a notice
    nobody ever sees."""
    import random

    from notice_extractor.utils.search import (fuzzy_contains,
                                               normalize_ocr_text)

    def naive(text, keywords, ratio):
        """The loop this replaced: every keyword, no skipping."""
        normalized = normalize_ocr_text(text)
        if not normalized:
            return 0.0, ""
        best, best_kw = 0.0, ""
        for keyword in keywords:
            score = fuzzy_contains(normalized, normalize_ocr_text(keyword),
                                   ratio)
            if score > best:
                best, best_kw = score, keyword
        return best, best_kw

    random.seed(11)
    pieces = ["જાહેર", "નોટિસ", "ચેતવણી", "નોટીસ", "public", "notice",
              "warning", "tender", "હરાજી", "મિલકત", "jaher", "chetavni",
              "ટેન્ડર", "42", "જાહર", "નોિટસ", "PUBLIC", "NOTICE", ""]
    cases = [" ".join(random.choice(pieces)
                      for _ in range(random.randint(1, 8)))
             for _ in range(1500)]

    was = pne.active_notice_type()
    checked = 0
    try:
        for label in pne.NOTICE_TYPE_CHOICES:
            pne.set_notice_type(label)
            keywords = pne.active_strict_keywords()
            for text in cases:
                assert pne.match_notice_text(text, False) == \
                    naive(text, keywords, pne.FUZZY_MATCH_RATIO), text
                checked += 1
        for text in cases:
            assert pne.match_negative_text(text) == \
                naive(text, pne.NEGATIVE_KEYWORDS,
                      pne.NEGATIVE_FUZZY_RATIO), text
            checked += 1
    finally:
        pne.set_notice_type({"notice": "જાહેર નોટિસ",
                             "chetavni": "જાહેર ચેતવણી"}.get(was, "All"))
    print(f"ok  keyword script guard identical over {checked} comparisons")


def test_console_survives_gujarati_output():
    """Printing this app's own text must not kill the process.

    Everything here is named in Gujarati, and Windows hands a redirected or
    piped stream the ANSI codepage - so `qa_run.py > report.txt` died on its
    eighth line with the extraction still running."""
    import subprocess

    script = ("import sys; sys.path.insert(0, %r);"
              "import notice_extractor;"
              "print('\\u0a9c\\u0abe\\u0ab9\\u0ac7\\u0ab0 "
              "\\u0aa8\\u0acb\\u0a9f\\u0abf\\u0ab8 \\u2715')" % ROOT)
    environment = dict(os.environ)
    # Force the failing configuration rather than inheriting a lucky one.
    environment.pop("PYTHONIOENCODING", None)
    environment.pop("PYTHONUTF8", None)
    environment["PYTHONLEGACYWINDOWSSTDIO"] = "1"
    done = subprocess.run([sys.executable, "-X", "utf8=0", "-c", script],
                          capture_output=True, env=environment, timeout=60)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-400:]
    print("ok  Gujarati output survives a redirected console")


def test_fuzzy_contains_pruning_changes_no_result():
    """fuzzy_contains skips windows whose cheap upper bound is already below
    the threshold.  That is only allowed to be faster, never different - and
    an earlier attempt at this (swapping the matcher's sequences) DID change
    results, because difflib.ratio() is not symmetric."""
    import difflib
    import random
    from notice_extractor.utils.search import fuzzy_contains

    def unpruned(haystack, needle, min_ratio):
        if not haystack or not needle:
            return 0.0
        if needle in haystack:
            return 1.0
        n = len(needle)
        if len(haystack) < max(3, int(n * min_ratio)):
            return 0.0
        best = 0.0
        for width in (n, n + 1, n - 1):
            if width < 3:
                continue
            for start in range(0, max(1, len(haystack) - width + 1)):
                ratio = difflib.SequenceMatcher(
                    None, needle, haystack[start:start + width]).ratio()
                best = max(best, ratio)
        return best if best >= min_ratio else 0.0

    guj = "જાહેરનોટિસચેતવણીમિલકતસર્વેનંબરમાલિકહકદાવો"
    lat = "publicnoticelandpropertysurveynumberownerclaim"
    rng = random.Random(11)
    needles = ["જાહેરનોટિસ", "જાહેરચેતવણી", "publicnotice", "notice"]
    checked = 0
    for _ in range(90):
        pool = rng.choice([guj, lat, guj + lat])
        text = "".join(rng.choice(pool)
                       for _ in range(rng.randint(5, 140)))
        if rng.random() < 0.4:                  # plant a near-miss
            nd = rng.choice(needles)
            cut = rng.randint(0, max(0, len(nd) - 1))
            text += nd[:cut] + nd[cut + 1:]
        for needle in needles:
            for threshold in (0.70, 0.80, 0.90):
                assert unpruned(text, needle, threshold) == \
                    fuzzy_contains(text, needle, threshold), \
                    (text[-40:], needle, threshold)
                checked += 1
    print(f"ok  fuzzy pruning identical over {checked} comparisons")


def test_page_sweep_looks_for_every_active_notice_type():
    """The full-page template sweep uses a capped list of templates.  Every
    જાહેર ચેતવણી template is appended after every નોટિસ spelling in two
    fonts, so the old blind templates[:6] meant the sweep never looked for
    ચેતવણી in ANY newspaper - "All" quietly meant "notices only"."""
    from notice_extractor import core, scrapers

    class Quiet:
        def __getattr__(self, _n):
            return lambda *a, **kw: None

    core.register_newspapers(scrapers.load_all())
    papers = [c for c in core.NEWSPAPER_REGISTRY.values()
              if c is not core.local_pdf_extractor()]
    assert papers, "no online newspapers registered"
    try:
        for label, want_notice, want_chetavni in (
                ("All", True, True),
                ("જાહેર નોટિસ", True, False),
                ("જાહેર ચેતવણી", False, True)):
            core.set_notice_type(label)
            for cls in papers:
                verifier = cls.pipeline_cls(reporter=Quiet(),
                                            ocr_engine=None).verifier
                families = {verifier._family_of(lab)
                            for lab, _t in verifier.scan_templates}
                assert len(verifier.scan_templates) <= core.PAGE_SCAN_TEMPLATES
                assert ("notice" in families) is want_notice, \
                    (cls.display_name, label, families)
                assert ("chetavni" in families) is want_chetavni, \
                    (cls.display_name, label, families)
    finally:
        core.set_notice_type("All")
    print("ok  page sweep covers both notice types in All mode")


def test_sweep_gate_only_where_a_real_template_backs_it():
    """The OCR sweep may only be skipped on a low template score when every
    notice type being looked for HAS a real cropped template.  Without one,
    a low score means "no template", not "no notice", and skipping would
    quietly lose the notices the sweep exists to find."""
    from notice_extractor import core, scrapers

    class Quiet:
        def __getattr__(self, _n):
            return lambda *a, **kw: None

    try:
        seen = {}
        scrapers.load_all()
        paper = core.NEWSPAPER_REGISTRY.get("Sandesh")
        prefixes = paper.pipeline_cls.embedded_prefixes if paper else None
        for label in core.NOTICE_TYPE_CHOICES:
            core.set_notice_type(label)
            verifier = core.HeaderTemplateVerifier(
                core.DetectionConfig(), Quiet(), embedded_prefixes=prefixes)
            mode = core.active_notice_type()
            wanted = ({"notice", "chetavni"} if mode == "all" else {mode})
            expected = wanted <= verifier.embedded_families
            assert verifier.gate_is_calibrated is expected, mode
            seen[mode] = (sorted(verifier.embedded_families),
                          verifier.gate_is_calibrated)

        # Whatever templates ship, these two invariants must hold.
        assert seen["notice"][1] is True, \
            "notice has real crops embedded - the gate should be on"
        if "chetavni" not in seen["chetavni"][0]:
            assert seen["chetavni"][1] is False and seen["all"][1] is False, \
                ("no real ચેતવણી crop is embedded, so the sweep must NOT be "
                 "skipped in chetavni or All mode")
    finally:
        core.set_notice_type("All")
    print(f"ok  sweep gate follows the real templates: {seen}")


def test_page_budget_is_shared_not_multiplied():
    """Pages in flight are a global budget.  If every edition took its own
    full pool, eight of them would hold half a gigabyte of decoded pages
    waiting for a detect slot only six can have."""
    from notice_extractor import core

    original = core.ACTIVE_AGENTS[0]
    try:
        core.ACTIVE_AGENTS[0] = 1
        alone = core.page_workers()
        assert alone == min(core.PAGE_WORKERS_MAX, core.DETECT_CONCURRENCY), \
            alone
        for agents in (1, 2, 4, 6, 8, 12, 40):
            core.ACTIVE_AGENTS[0] = agents
            per = core.page_workers()
            assert per >= 1, per                     # never stalls a run
            assert per <= core.PAGE_WORKERS_MAX, per
            # Crowded runs must not each claim the whole budget.
            if agents >= core.DETECT_CONCURRENCY:
                assert per == 1, (agents, per)
        # A lone edition must actually use the machine, not one page at a time.
        assert alone > 1, alone
    finally:
        core.ACTIVE_AGENTS[0] = original
    print(f"ok  page budget: 1 edition -> {alone} pages, "
          f"8 editions -> 1 page each")


def test_machine_junk_stays_out_of_the_project():
    """A Chromium profile (~300 files) and .pyc caches are machine-local
    junk: they must live in the machine cache, not in the source tree."""
    from notice_extractor import config

    for path in (config.BROWSER_PROFILE_DIR, config.PYCACHE_DIR):
        assert not config.is_inside_project(path), f"{path} is in the project"
        assert not config.is_inside_data(path), f"{path} is in data/"
    assert config.is_inside_project(config.PACKAGE_DIR)

    # The sweep must find and remove a stray __pycache__ anywhere in the tree.
    stray = os.path.join(config.PACKAGE_DIR, "__pycache__")
    os.makedirs(stray, exist_ok=True)
    open(os.path.join(stray, "probe.pyc"), "wb").write(b"x")
    removed = config.clear_pycache()
    assert not os.path.exists(stray), removed
    print("ok  browser profile and .pyc caches live outside the project")


def test_browser_session_is_optional():
    """Every machine must be able to import the scrapers, whether or not
    Playwright is installed - the browser is used, never required."""
    from notice_extractor.scrapers import browser_session

    channel = browser_session.default_browser_channel()
    assert channel in ("", "chrome", "msedge"), channel
    assert browser_session.same_site(
        "https://epaper.bhaskarassets.com/x", "bhaskarassets.com")
    assert not browser_session.same_site("https://evil.com/x",
                                         "bhaskarassets.com")
    # Page renders are kept, site furniture is not.
    keep = browser_session._default_image_filter
    assert keep("https://epaper.bhaskarassets.com/thumb/0x0/db/x_lll.webp")
    assert not keep("https://images.bhaskarassets.com/logo.png")
    assert not keep("https://images.bhaskarassets.com/thumb/120x0/x.jpg")
    print("ok  browser session helpers work without launching a browser")


def test_half_copy_is_not_a_rejection():
    """Half Copy teaches SEGMENTATION, never that the notice was wrong.

    Training a half crop as a relevance negative would teach the classifier
    to reject the notices it exists to find - the recall bug this whole
    split of signals is here to prevent."""
    import tempfile
    from notice_extractor import config
    from notice_extractor.utils import feedback as fb

    folder = tempfile.mkdtemp(prefix="pne-half-")
    original = config.DATA_DIR
    config.DATA_DIR = folder
    try:
        text = ("આથી જાહેર જનતાને જણાવવાનું કે સદરહુ મિલકત અંગે "
                "કોઈપણ પ્રકારનો હક્ક હિસ્સો")
        result = _feedback_result(pne, text, confidence=70)
        result.newspaper = "Sandesh"
        result.method = "box+template"
        result.page_rect = (100, 200, 300, 400)
        result.page_size = (1500, 2000)

        fb.record_half_crop(result, "bottom", (1500, 2000))
        records = fb.load_records()
        assert len(records) == 1, records
        assert records[0]["feedback"] == "half_crop"
        assert records[0]["direction"] == "bottom"
        assert records[0]["crop"]["width"] == 300, records[0]["crop"]
        assert records[0]["page_dimensions"]["height"] == 2000

        # ...and the CLASSIFIER reads it as a positive, not a negative.
        model = fb.build_model(records)
        assert model["negative_examples"] == 0, model
        assert model["positive_examples"] == 1, model
        assert fb.score(records[0]["normalized_text"], model) == 0.0

        # One report is not a rule; two of the same kind are.
        assert fb.expansion_hint("Sandesh", "box+template", 300, 400,
                                 (1500, 2000)) == ""
        fb.record_half_crop(result, "bottom", (1500, 2000))
        assert fb.expansion_hint("Sandesh", "box+template", 300, 400,
                                 (1500, 2000)) == "bottom"
        # A confirmed crop in the same class withdraws the hint.
        fb.record_crop_confirmed(result)
        fb.record_crop_confirmed(result)
        assert fb.expansion_hint("Sandesh", "box+template", 300, 400,
                                 (1500, 2000)) == ""
        half, total, rate = fb.half_crop_rate()
        assert (half, total) == (2, 2) and rate == 1.0, (half, total, rate)
    finally:
        config.DATA_DIR = original
        shutil.rmtree(folder, ignore_errors=True)
    print("ok  half copy trains segmentation, never relevance")


def test_missing_direction_needs_an_unruled_edge():
    """A crop that ends on its own printed border is COMPLETE.

    "Is there ink just outside?" is not the question - on a notice board
    there always is, because the next notice is there.  Measured on 30 real
    Sandesh crops, that test alone called 28 of 30 complete crops short."""
    import numpy as np
    from notice_extractor.utils import feedback as fb

    # A page at the size detection actually works on (1500 px wide): the
    # ruling-line morphology scales with the page, so a toy 400 px page
    # classifies ordinary words as rules and the fixture proves nothing.
    page = np.full((2000, 1500), 255, dtype=np.uint8)
    cv2 = pne.cv2
    for top in (100, 800):
        cv2.rectangle(page, (100, top), (400, top + 600), 0, 3)
        for line in range(22):
            y = top + 22 + line * 26
            if y > top + 590:
                break
            for word in range(5):
                x = 116 + word * 60
                cv2.line(page, (x, y), (x + 40, y), 0, 7)
    complete = (100, 100, 300, 600)
    assert fb.missing_direction(page, complete, (1500, 2000)) == "unknown", \
        "a fully ruled crop was called incomplete"
    # The same notice cut short: no rule under it, its own text continues.
    short = (100, 100, 300, 380)
    assert fb.missing_direction(page, short, (1500, 2000)) == "bottom", \
        "a crop cut mid-text was not spotted"
    print("ok  missing direction needs an unruled edge, not just nearby ink")


def test_a_stalled_agent_does_not_kill_the_others():
    """The 900 s timeout bug: ONE batch deadline abandoned every agent that
    was still running, mid-edition, still publishing notices."""
    import queue as _queue
    import threading as _threading
    from datetime import date as _date
    from notice_extractor.agents import processor

    stall, ceiling = pne.AGENT_STALL_SECONDS, pne.AGENT_TIMEOUT_SECONDS
    pne.AGENT_STALL_SECONDS, pne.AGENT_TIMEOUT_SECONDS = 3, 60

    def make(name, behaviour):
        class Fake(pne.BaseNewspaperExtractor):
            display_name = name
            editions = ("e1",)
            default_edition = "e1"

            def __init__(self, broad=False):
                self.current_issue_date = ""

            def extract_all(self, pairs, reporter, finalize=True,
                            start_result_id=0):
                behaviour(reporter)
        return Fake

    def alive(reporter):
        for page in range(1, 7):
            time.sleep(1.0)
            reporter.progress(page, 6)      # proof of life
        reporter.result(pne.NoticeResult(
            result_id=1, page_number=1, index_on_page=1, image_bgr=None,
            confidence=70, method="x"))

    def stuck(reporter):
        time.sleep(45)

    try:
        messages = _queue.Queue()
        reporter = pne.ProgressReporter(messages, _threading.Event())
        jobs = [(make("SlowPaper", alive), "e1", _date.today(), "u"),
                (make("StuckPaper", stuck), "e1", _date.today(), "u")]
        started = time.perf_counter()
        summary = processor.run_jobs(jobs, reporter)
        elapsed = time.perf_counter() - started
        assert summary.total == 1, \
            f"the live agent's work was thrown away ({summary.total})"
        assert len(summary.skipped) == 1 and \
            "StuckPaper" in summary.skipped[0], summary.skipped
        assert elapsed < 25, f"waited far too long: {elapsed:.0f}s"
    finally:
        pne.AGENT_STALL_SECONDS, pne.AGENT_TIMEOUT_SECONDS = stall, ceiling
    print("ok  a stalled agent is dropped alone; live agents keep their work")


def test_cache_lives_on_the_apps_own_drive_and_is_bounded():
    """The disk-full failures: the cache used to go to the system temp
    drive and was only cleaned by atexit (which a killed agent never runs)."""
    import tempfile
    from notice_extractor import config

    folder = tempfile.mkdtemp(prefix="pne-cache-")
    original = config.DATA_DIR
    config.DATA_DIR = folder
    try:
        cache = pne.cache_dir()
        assert os.path.abspath(cache).startswith(os.path.abspath(folder)), \
            f"cache escaped the data folder: {cache}"
        for index in range(6):
            with open(os.path.join(cache, f"page{index}"), "wb") as handle:
                handle.write(b"x" * 1024)
        assert pne.cache_bytes() >= 6 * 1024
        freed = pne.evict_cache(2 * 1024)
        assert freed >= 4 * 1024, freed
        assert pne.cache_bytes() <= 2 * 1024, pne.cache_bytes()
        # Nothing outside the cache is ever a candidate.
        keep = os.path.join(folder, "feedback.jsonl")
        with open(keep, "w", encoding="utf-8") as handle:
            handle.write("{}")
        pne.evict_cache(0)
        assert os.path.exists(keep), "eviction touched user data"
    finally:
        config.DATA_DIR = original
        shutil.rmtree(folder, ignore_errors=True)
    print("ok  page cache is on the app's drive, bounded, and never eats data")


def main() -> int:
    tests = [
        test_notice_type_toggle_filters_matching,
        test_multi_word_search_matches_every_token,
        test_search_result_flag_drives_the_count,
        test_target_date_resolves,
        test_divya_bhaskar_page_list_parsing,
        test_divya_bhaskar_login_detection,
        test_status_log_pane_stays_visible,
        test_gallery_layout_is_coalesced,
        test_notice_type_buttons_apply_on_click,
        test_setup_components_are_all_probeable,
        test_run_data_is_transient_but_the_login_is_not,
        test_tessdata_is_found_wherever_it_lives,
        test_startup_never_fails_silently,
        test_doctor_reports_the_environment,
        test_empty_hint_goes_away_when_a_notice_arrives,
        test_empty_hint_is_actually_visible,
        test_search_bar_hides_remove_until_there_is_something_to_remove,
        test_recent_search_history_round_trips_through_the_ui,
        test_a_match_without_boxes_is_still_shown,
        test_searching_filters_the_gallery_by_itself,
        test_notice_type_click_filters_what_is_on_screen,
        test_saving_and_counting_follow_the_filter,
        test_a_late_log_line_cannot_undo_the_shutdown_wipe,
        test_a_new_run_unsticks_a_background_read,
        test_one_bad_message_does_not_kill_the_pump,
        test_normal_cards_ask_one_question_only,
        test_review_queue_is_the_only_place_you_confirm,
        test_learning_needs_repetition_and_protects_recall,
        test_short_negative_keywords_do_not_veto_on_a_name,
        test_status_log_starts_closed_and_comes_back_whole,
        test_help_guide_opens_without_breaking_scrolling,
        test_the_ocr_header_test_reads_a_shallower_band,
        test_the_preview_drops_a_notice_the_filter_hid,
        test_recording_the_header_family_changes_no_detection,
        test_the_header_family_reaches_the_notice,
        test_notice_type_is_read_off_the_crop,
        test_one_category_covers_notice_and_chetavni,
        test_keyword_script_guard_changes_no_result,
        test_console_survives_gujarati_output,
        test_fuzzy_contains_pruning_changes_no_result,
        test_page_sweep_looks_for_every_active_notice_type,
        test_sweep_gate_only_where_a_real_template_backs_it,
        test_page_budget_is_shared_not_multiplied,
        test_machine_junk_stays_out_of_the_project,
        test_browser_session_is_optional,
        test_half_copy_is_not_a_rejection,
        test_missing_direction_needs_an_unruled_edge,
        test_a_stalled_agent_does_not_kill_the_others,
        test_cache_lives_on_the_apps_own_drive_and_is_bounded,
        test_newspaper_plugins_load,
        test_plugins_have_no_dangling_names,
        test_plugin_loader_is_idempotent,
        test_gallery_routes_interleaved_results,
        test_gallery_creates_section_on_demand,
        test_reporter_streams_immediately,
        test_agent_retries_transient_failures_but_never_credentials,
        test_ocr_chain_prefers_gujarati,
        test_ocr_engine_is_cached,
        test_detect_gate_bounds_concurrency,
        test_detect_gate_release_round_trips,
        test_search_finds_and_boxes_gujarati,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:
            failed += 1
            print(f"FAIL  {test.__name__}: {type(exc).__name__}: {exc}")
        sys.stdout.flush()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.stdout.flush()
    if _ROOT is not None:
        _ROOT.destroy()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
