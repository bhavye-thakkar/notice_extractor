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
        test_fuzzy_contains_pruning_changes_no_result,
        test_page_sweep_looks_for_every_active_notice_type,
        test_sweep_gate_only_where_a_real_template_backs_it,
        test_page_budget_is_shared_not_multiplied,
        test_machine_junk_stays_out_of_the_project,
        test_browser_session_is_optional,
        test_newspaper_plugins_load,
        test_plugins_have_no_dangling_names,
        test_plugin_loader_is_idempotent,
        test_gallery_routes_interleaved_results,
        test_gallery_creates_section_on_demand,
        test_reporter_streams_immediately,
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
