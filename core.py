#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
 Public Notice Extractor - Gujarat Samachar + Sandesh + Divya Bhaskar
===============================================================================
A classic Windows-style desktop utility that extracts Public Notices
(જાહેર નોટિસ) from Gujarati e-papers.

The URL is pre-filled from the newspaper + date picker (Gujarat Samachar's
archive covers the last 7 days; Sandesh allows any date), or the user can
paste one directly:

    https://epaper.gujaratsamachar.com/ahmedabad/07-08-2026/1
    https://sandesh.com/epaper/ahmedabad?date=2026-08-07&page=1
    https://www.divyabhaskar.co.in/epaper?edition=ahmedabad&date=2026-08-07

A downloaded e-paper PDF can also be opened directly ("Open PDF..."): every
page is rendered and scanned exactly like the online editions.

The application then:
  1. Discovers every page of that edition automatically.
  2. Downloads every page at the highest available resolution.
  3. Visually scans every page for Public Notice boxes.
  4. Crops each detected notice and shows it in a thumbnail gallery.
  5. Lets the user preview (zoom / scroll / fit) and save the crops.

Detection (v1.2) is computer-vision first and works OUT OF THE BOX with only
pip-installable libraries:
  * Real "જાહેર નોટિસ" header images, cropped from an actual Gujarat
    Samachar page, are EMBEDDED in this file (base64 PNGs) and used as the
    primary multi-scale matching templates - the newspaper's own typeface,
    no fonts or OCR required.
  * Morphological line extraction -> rectangular box candidates; each
    candidate's header strip is checked against the templates.
  * A full-page template sweep recovers notices with broken borders.
  * OPTIONAL extra safety net: if Gujarati OCR happens to be available
    (Tesseract with 'guj' data, or 'pip install winsdk' + the Windows
    Gujarati language pack), it verifies borderline strips and deep-scans
    pages where nothing was found.  It is never required.

Architecture: every newspaper gets its own independent extractor class
registered in NEWSPAPER_REGISTRY.  The GUI never needs to change when a new
newspaper (Mumbai Samachar, Akila, ...) is added - implement a subclass of
BaseNewspaperExtractor and register it.

Third-party dependencies (pip only - no external programs needed):
    pip install opencv-python numpy pillow
Optional OCR safety net (also pip only):
    pip install winsdk        (uses Windows built-in OCR; add the Gujarati
                               language pack in Windows Settings)

Single-file by design.  Sections:
    1. Imports
    2. Constants & Configuration
    3. Utilities
    4. Progress reporting / worker plumbing
    5. Downloader
    6. Page discovery (Gujarat Samachar, 6b Sandesh, 6c Divya Bhaskar)
    7. Detection pipeline (template verifier, box detector, pipeline)
    8. Extractor base class + Gujarat Samachar extractor + registry
    9. GUI (log, gallery, preview, main application window)
   10. Main entry point
===============================================================================
"""

# =============================================================================
# 1. IMPORTS
# =============================================================================

import os
import re
import io
import sys
import glob
import json
import time
import base64
import contextlib
import functools
import queue
import atexit
import shutil
import hashlib  # page-signature checks (Nav Gujarat Samay cover guard)
import tempfile
import threading
import traceback
import concurrent.futures
import subprocess
import calendar as _calendar
import datetime
from datetime import date, timedelta
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar
from html.parser import HTMLParser
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple, Type

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tkinter.font as tkfont

# --- package modules (no third-party imports, so they are always safe) -------
from . import config
from .ui.app import LOG_PANE_MIN_WIDTH, LOG_PANE_WIDTH, StatusLogPanel
from .utils import logger as run_logger
from .utils.search import (FUZZY_MATCH_RATIO, fuzzy_contains,  # noqa: F401
                           normalize_ocr_text, search_notice)

# --- Third-party imports (guarded so we can show a friendly dialog) ----------
_MISSING_DEPENDENCIES: List[str] = []
try:
    import numpy as np
except ImportError:
    _MISSING_DEPENDENCIES.append("numpy")
    np = None  # type: ignore
try:
    import cv2
except ImportError:
    _MISSING_DEPENDENCIES.append("opencv-python")
    cv2 = None  # type: ignore
try:
    from PIL import Image, ImageTk, ImageDraw, ImageFont
    from PIL import features as pil_features
except ImportError:
    _MISSING_DEPENDENCIES.append("pillow")
    Image = ImageTk = ImageDraw = ImageFont = pil_features = None  # type: ignore
try:
    import pymupdf as _pymupdf_probe  # noqa: F401
except ImportError:
    try:
        import fitz as _pymupdf_probe  # noqa: F401
    except ImportError:
        _MISSING_DEPENDENCIES.append("pymupdf")

# Optional PDF rendering (local e-paper PDF files):  pip install pymupdf
try:
    import pymupdf as fitz  # type: ignore
    _HAVE_FITZ = True
except ImportError:
    try:
        import fitz  # type: ignore  # older PyMuPDF releases
        _HAVE_FITZ = True
    except ImportError:
        fitz = None  # type: ignore
        _HAVE_FITZ = False

# Optional OCR support.  Two engines, auto-fallback:
#   1. Tesseract (pytesseract + the Tesseract program with 'guj' data)
#   2. Windows built-in OCR (winsdk / winrt + the Gujarati language pack)
try:
    import pytesseract  # type: ignore
    _HAVE_PYTESSERACT = True
except ImportError:
    pytesseract = None  # type: ignore
    _HAVE_PYTESSERACT = False


# =============================================================================
# 2. CONSTANTS & CONFIGURATION
# =============================================================================

APP_NAME = "Public Notice Extractor"
APP_VERSION = "1.36"
APP_TITLE = f"{APP_NAME} v{APP_VERSION}"

# Newspaper-dropdown entry that runs every online newspaper in one go.
ALL_NEWSPAPERS_LABEL = "All Newspapers"
# Hard cap so a wide date range cannot start a run that never ends.
MAX_RANGE_DAYS = 31

# Sandesh's e-paper viewer is a JavaScript app; page images are discovered at
# runtime from the raw page HTML (see sandesh_discover_pages).
SANDESH_MAX_PAGES = 40

# Everything the app needs (and the optional OCR extra), pip-installable.
# Note: upgrading pillow does NOT bring in the raqm shaping engine - its
# Windows wheels have never bundled libraqm and there is no 'pillow[raqm]'
# extra on PyPI.  See setup_ocr.py --raqm; OCR matters far more here.
DEPENDENCY_PACKAGES: Tuple[str, ...] = ("opencv-python", "numpy", "pillow",
                                        "pymupdf")
# Divya Bhaskar's viewer only hands over its page list and access token to a
# real browser, so browser automation is a first-class dependency, not an
# extra.  The browser binary itself is fetched by `playwright install`, which
# pip_install_dependencies() runs as its own step.
BROWSER_PACKAGES: Tuple[str, ...] = ("playwright",)
# Auto-login reads the browser's encrypted cookie store; on Windows the
# newer cookies are AES-GCM encrypted (pycryptodome provides the cipher).
OPTIONAL_AUTOLOGIN_PACKAGES: Tuple[str, ...] = (
    ("pycryptodome",) if sys.platform.startswith("win") else ())
# Windows OCR bindings.  'winsdk' is the old all-in-one package and its last
# release only ships wheels up to CPython 3.12 - on 3.13+ pip cannot install
# it at all (it tries a source build and fails).  The maintained replacement is
# the split 'winrt-Windows.*' projection set, which does have current wheels.
# Both import as a package the OCR engine below knows how to find.
WINRT_OCR_PACKAGES: Tuple[str, ...] = (
    "winrt-Windows.Media.Ocr",
    "winrt-Windows.Globalization",
    "winrt-Windows.Graphics.Imaging",
    "winrt-Windows.Storage.Streams",
    "winrt-Windows.Foundation",
    "winrt-Windows.Foundation.Collections",
)
WINDOWS_OCR_PACKAGES: Tuple[str, ...] = (
    ("winsdk",) if sys.version_info < (3, 13) else WINRT_OCR_PACKAGES)
WINRT_INSTALL_HINT = "pip install " + " ".join(WINDOWS_OCR_PACKAGES)

OPTIONAL_PACKAGES: Tuple[str, ...] = (
    (WINDOWS_OCR_PACKAGES + ("pycryptodome",))
    if sys.platform.startswith("win") else ())

# The OCR client library.  'easyocr' is deliberately NOT here: it drags in
# torch (~2 GB) and ships no Gujarati model, so it would be a large download
# that cannot improve detection.  setup_ocr.py can add it on request.
OCR_PIP_PACKAGES: Tuple[str, ...] = ("pytesseract",)

# --- OCR backend selection ---------------------------------------------------
# The engines are tried in this order and the first one that can actually read
# Gujarati wins.  Set a flag to False to skip that rung entirely.
USE_WINDOWS_OCR = True      # 1. Windows.Media.Ocr  (needs winsdk + 'gu' pack)
USE_TESSERACT_OCR = True    # 2. Tesseract          (needs guj.traineddata)
USE_EASYOCR = True          # 3. EasyOCR            (see note below)
# An engine without Gujarati is worse than no engine: this app matches only
# Gujarati keywords ("જાહેર નોટિસ" - the English words are never printed), so a
# Latin-only engine would burn OCR time on every strip for zero extra hits.
# Set to False to accept a Latin-only engine anyway.
OCR_REQUIRE_GUJARATI = True

# Gujarati language data can live in a 'tessdata' folder next to this program,
# which avoids needing administrator rights to write into Program Files.
LOCAL_TESSDATA_DIRNAME = "tessdata"
# Official upstream model, ~2 MB (setup_ocr.py downloads it on request).
GUJ_TRAINEDDATA_URL = ("https://github.com/tesseract-ocr/tessdata/raw/main/"
                       "guj.traineddata")


def app_dir() -> str:
    """The project folder (the one holding notice_extractor/ and tessdata/).

    Derived from this file's location, not from sys.argv[0]: the app has to
    find tessdata and its data folder the same way whether it was started by
    the launcher, by `python -m notice_extractor.main`, or by a shortcut."""
    return config.PROJECT_ROOT


def local_tessdata_dir() -> str:
    return os.path.join(app_dir(), LOCAL_TESSDATA_DIRNAME)


# Where Tesseract is installed to when this app installs it: on the SAME
# drive as the program, not the system drive.
TESSERACT_DIRNAME = "Tesseract-OCR"


def preferred_tesseract_dir() -> str:
    """Install target for Tesseract - <app drive>\\Tesseract-OCR.

    Keeping it on the program's own drive means a copy of the app on E: is
    self-contained and needs no administrator rights on C:."""
    drive = os.path.splitdrive(os.path.abspath(app_dir()))[0]
    if drive:
        return os.path.join(drive + os.sep, TESSERACT_DIRNAME)
    return os.path.join(app_dir(), TESSERACT_DIRNAME)


def _tesseract_candidates() -> Tuple[str, ...]:
    """Every place tesseract.exe might live, best first.

    The app's own drive and folder come before the system-drive installs, so
    a local copy always wins over a stale C: one."""
    names = ("tesseract.exe" if sys.platform.startswith("win")
             else "tesseract")
    paths = [
        os.path.join(preferred_tesseract_dir(), names),
        os.path.join(app_dir(), TESSERACT_DIRNAME, names),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(
            r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"),
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/opt/homebrew/bin/tesseract",
    ]
    seen: set = set()
    unique = []
    for path in paths:
        if path and path not in seen:
            seen.add(path)
            unique.append(path)
    return tuple(unique)


TESSERACT_CANDIDATE_PATHS: Tuple[str, ...] = _tesseract_candidates()

# Notice-header keywords.  The Gujarat Samachar header is "જાહેર નોટિસ" -
# English "PUBLIC NOTICE" is NOT printed, so strict mode is Gujarati-only.
# "broad" additionally matches other legal notices (UI checkbox).
JAHER_NOTICE_KEYWORDS: Tuple[str, ...] = (
    "જાહેર નોટિસ", "જાહેર નોટીસ", "જાહેરનોટિસ", "જાહેર નોટીશ",
)
# જાહેર ચેતવણી (public warning) prints as its own boxed notice type.
CHETAVNI_KEYWORDS: Tuple[str, ...] = (
    "જાહેર ચેતવણી", "જાહેરચેતવણી", "જાહેર ચેતવણિ",
)
STRICT_KEYWORDS: Tuple[str, ...] = JAHER_NOTICE_KEYWORDS + CHETAVNI_KEYWORDS

# Run-level notice-type filter, set from the GUI toggle before a run starts.
# All agents of one run share it (one run = one choice, so a plain global).
NOTICE_TYPE_CHOICES: Tuple[str, ...] = ("All", "જાહેર નોટિસ", "જાહેર ચેતવણી")
_notice_type = ["all"]                  # "all" | "notice" | "chetavni"


def set_notice_type(label: str) -> None:
    """Map the GUI toggle value onto the matcher filter."""
    if "ચેતવણ" in label:
        _notice_type[0] = "chetavni"
    elif "નોટ" in label:
        _notice_type[0] = "notice"
    else:
        _notice_type[0] = "all"


def active_notice_type() -> str:
    return _notice_type[0]


def active_strict_keywords() -> Tuple[str, ...]:
    mode = _notice_type[0]
    if mode == "notice":
        return JAHER_NOTICE_KEYWORDS
    if mode == "chetavni":
        return CHETAVNI_KEYWORDS
    return STRICT_KEYWORDS
BROAD_KEYWORDS: Tuple[str, ...] = STRICT_KEYWORDS + (
    "કાનૂની નોટિસ", "નામ ફેરફાર", "જાહેર સૂચના",
)

# Headers that are NOT public notices but share words and typography with
# "જાહેર નોટિસ" - tenders (નિવિદા), e-auction / possession / sale-by-auction
# notices and recruitment ads.  A detection whose header matches this
# vocabulary better than the notice header itself is DROPPED (v1.8) - see
# NoticeDetectionPipeline._reject_negatives().
NEGATIVE_KEYWORDS: Tuple[str, ...] = (
    # tenders
    "ટેન્ડર નોટિસ", "ટેન્ડર નોટીસ", "ઈ-ટેન્ડર", "ઇ-ટેન્ડર", "ટેન્ડર",
    "જાહેર નિવિદા", "ઈ-નિવિદા", "નિવિદા",
    "tender", "e-tender",
    # auctions / possession / sale-by-auction (bank & court sales)
    "જાહેર હરાજી", "ઈ-હરાજી", "ઇ-હરાજી", "હરાજી", "લિલામ",
    "e-auction", "auction",
    "કબજા નોટિસ", "કબ્જા નોટિસ", "વેચાણ નોટિસ", "વેચાણ નોટીસ",
    # recruitment advertisements
    "ભરતી જાહેરાત", "ભરતી",
)
NEGATIVE_FUZZY_RATIO = 0.84       # stricter than the positive match ratio
NEGATIVE_TEMPLATE_MIN = 0.42      # template-veto floor (font-vs-font scores)
NEGATIVE_TEMPLATE_MARGIN = 0.05   # neg must BEAT the font positive by this
NEGATIVE_TRUST_POS = 0.72         # a positive score this high is trusted
# Embedded STRONG negatives: variant pills like "જાહેર નોટિસમાં સુધારો"
# are real જાહેર નોટિસ prints and would survive every text test, so they
# are located by a dedicated full-page template scan and vetoed by
# position (the pill may be wider than its detected box).
STRONG_NEGATIVE_PAGE_THRESHOLD = 0.72
STRONG_NEGATIVE_SCALES: Tuple[int, ...] = (30, 33, 36, 40, 44, 48)
# OCR-level override: reject these even when "જાહેર નોટિસ" is present.
NEGATIVE_OVERRIDE_KEYWORDS: Tuple[str, ...] = (
    "નોટિસમાં સુધારો", "નોટીસમાં સુધારો", "નોટિસમાં સુધારા",
)
# Word pairs used by the full-page OCR sweep ("જાહેર" ... "નોટિસ" appearing
# next to each other on one line).  (first-word fragment, second-word fragment)
SWEEP_WORD_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("જાહેર", "નોટ"),
    ("જાહેર", "ચેત"),          # જાહેર ચેતવણી (public warning)
)
# Single OCR "words" that already contain the whole title.
SWEEP_SINGLE_WORDS: Tuple[str, ...] = ("જાહેરનોટ",)
# FUZZY_MATCH_RATIO (tolerance for OCR errors in keywords) is defined with the
# matcher itself, in utils/search.py, and imported at the top of this file.
OCR_STRIP_TARGET_HEIGHT = 72      # strips are upscaled to this before OCR
OCR_MAX_STRIPS_PER_PAGE = 90      # safety cap
# OCR calls per page that run at the same time ("multiple OCR agents").
# Kept modest so that several newspapers scanning in parallel do not spawn
# MAX_PARALLEL_JOBS x OCR_WORKERS threads all at once.
OCR_WORKERS = 4
# The full-page OCR sweep reads a page downscaled to this width.  Titles are
# display-size type and still read fine; Tesseract cost scales with pixels.
SWEEP_MAX_WIDTH = 1100

# Networking ------------------------------------------------------------------
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# --- proxy support (LAN / office networks) ----------------------------------
# A saved proxy (Tools > Network) wins; otherwise env vars / the system
# (Windows Internet Settings) proxy is used automatically.
PROXY_FILENAME = "network_proxy.txt"
_proxy_cache: Optional[str] = None


def _proxy_path() -> str:
    return config.session_file(PROXY_FILENAME)


def load_proxy(force: bool = False) -> str:
    """The configured proxy URL ('' = use the system/auto proxy)."""
    global _proxy_cache
    if _proxy_cache is None or force:
        value = ""
        try:
            with open(_proxy_path(), "r", encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            value = ""
        _proxy_cache = value
    return _proxy_cache


def save_proxy(proxy: str) -> str:
    global _proxy_cache
    with open(_proxy_path(), "w", encoding="utf-8") as fh:
        fh.write(proxy.strip())
    _proxy_cache = proxy.strip()
    return _proxy_path()


def build_proxy_handler() -> "urllib.request.ProxyHandler":
    """A ProxyHandler for the saved proxy, or the system/auto proxy."""
    proxy = load_proxy()
    if proxy:
        if "://" not in proxy:
            proxy = "http://" + proxy
        return urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    # No manual proxy -> urllib reads the OS / env proxy automatically.
    return urllib.request.ProxyHandler(urllib.request.getproxies())
HTTP_TIMEOUT_SECONDS = 25
HTTP_RETRIES = 2
HTTP_RETRY_DELAY_SECONDS = 1.5

# --- concurrency -------------------------------------------------------------
# Every edition runs as its own agent, all of them at once.  An edition spends
# most of its life waiting on the newspaper's server, so the agent count is
# bounded only by sockets and memory, NOT by cores.
#
# The CPU-bound part (template matching) is governed separately by
# DETECT_CONCURRENCY below, so adding agents never turns into CPU thrash.
MAX_PARALLEL_JOBS = 16        # ceiling on simultaneous edition agents
# Pages fetched ahead of the detector inside one job (bounded so a 40-page
# edition never holds 40 decoded pages in memory at once).
PAGE_PREFETCH = 3

#: Cap on pages per edition, for quick checks (main.py --pages N).  0 = all.
PAGE_LIMIT = [0]

_CPUS = os.cpu_count() or 4
# Measured on a 16-core box with the synthetic-page benchmark:
#   1 detect x 16 cv2 threads -> 0.33 pages/s   (cv2 scales badly on one image)
#   4 detects x  4 cv2 threads -> 0.78 pages/s
# so run a few detections side by side and give each a modest cv2 pool rather
# than one detection with every core.
# Measured on the 16-core box (synthetic-page benchmark, pages/s):
#   4 detects x 4 cv2 threads -> 0.78      6 x 3 -> 1.41      1 x 16 -> 0.33
# More, narrower detectors win: matchTemplate scales poorly across many
# threads on ONE image, so run more images with a small pool each.
DETECT_CONCURRENCY = max(2, min(8, (_CPUS * 3) // 8))
CV2_THREADS_PER_DETECT = max(2, round(_CPUS / DETECT_CONCURRENCY))
# Wall-clock budget for one edition agent before it is abandoned, and how many
# times a failed agent is retried (network flakiness is the common cause).
AGENT_TIMEOUT_SECONDS = 900
AGENT_RETRIES = 1


def resolve_job_workers(job_count: int) -> int:
    """Agents to run for `job_count` editions - all of them, up to the cap."""
    return max(1, min(MAX_PARALLEL_JOBS, job_count))


#: Gate around the CPU-heavy detect() call.  Shared by every agent so that N
#: editions cannot start N simultaneous full-page template sweeps.
_detect_gate = threading.BoundedSemaphore(DETECT_CONCURRENCY)
#: Threads that currently hold a detect slot, so a nested release is only
#: attempted by a thread that actually took one.
_detect_held = threading.local()


@contextlib.contextmanager
def detect_gate_held():
    """Hold a detect slot for the duration of the block."""
    with _detect_gate:
        _detect_held.on = True
        try:
            yield
        finally:
            _detect_held.on = False


@contextlib.contextmanager
def detect_gate_released():
    """Temporarily give the detect slot back (used while waiting on OCR).

    A no-op for a thread that is not holding one, so detect() stays callable
    directly from a test or a one-off script."""
    if not getattr(_detect_held, "on", False):
        yield
        return
    _detect_held.on = False
    _detect_gate.release()
    try:
        yield
    finally:
        _detect_gate.acquire()
        _detect_held.on = True

#: One OCR thread pool for the whole process.  The old code built a fresh
#: ThreadPoolExecutor for every page of every edition; at 40 pages x 12
#: editions that is 480 pool create/destroy cycles per run.
_ocr_pool: List["concurrent.futures.ThreadPoolExecutor"] = []
_ocr_pool_lock = threading.Lock()


def get_ocr_pool() -> "concurrent.futures.ThreadPoolExecutor":
    """The shared OCR worker pool, created on first use."""
    with _ocr_pool_lock:
        if not _ocr_pool:
            _ocr_pool.append(concurrent.futures.ThreadPoolExecutor(
                max_workers=max(4, _CPUS // 2),
                thread_name_prefix="ocr"))
        return _ocr_pool[0]


def shutdown_ocr_pool() -> None:
    """Release the shared OCR pool (called when the app closes)."""
    with _ocr_pool_lock:
        while _ocr_pool:
            try:
                _ocr_pool.pop().shutdown(wait=False)
            except Exception:
                pass

# Detection tuning ------------------------------------------------------------

@dataclass
class DetectionConfig:
    """All tunables of the visual detection pipeline in one place."""
    # Pages wider than this are downscaled for detection (crops are always
    # taken from the original full-resolution image).
    working_width: int = 1500

    # Candidate box geometry filters (fractions of working page size).
    box_min_w_frac: float = 0.055
    box_max_w_frac: float = 0.640
    box_min_h_px: int = 64
    box_max_h_frac: float = 0.750
    box_min_aspect: float = 0.22    # h / w
    box_max_aspect: float = 9.00

    # How much of a candidate's perimeter must lie on detected ruling lines.
    border_coverage_total: float = 0.58
    border_coverage_side: float = 0.30

    # Header strip: the region at the top of a candidate box that is checked
    # for the "જાહેર નોટિસ" title.
    strip_frac_of_box: float = 0.32
    strip_max_px: int = 170
    strip_min_px: int = 42

    # Template matching.  Scales start low so lower-resolution pages and
    # small single-column notices still match.
    template_render_px: int = 72          # base render height of templates
    strip_scales: Tuple[int, ...] = (14, 16, 18, 22, 26, 31, 37, 44, 52, 62)
    page_scan_scales: Tuple[int, ...] = (16, 20, 26, 33, 42)
    box_match_threshold: float = 0.58     # inside a verified candidate box
    page_match_threshold: float = 0.66    # stricter for the full-page sweep
    ocr_assist_low: float = 0.45          # borderline zone where OCR may help

    # De-duplication.
    nms_iou_threshold: float = 0.45
    nms_containment: float = 0.72

    # Crop padding (working-scale pixels).
    crop_padding: int = 6


DETECTION_CONFIG = DetectionConfig()

# No detection below this template score is ever shown, whatever a paper's
# own threshold is - it removes the ~0.60-0.64 borderline false positives
# (stray photos, sale/auction tables) the user sees as "extras".
GLOBAL_MIN_ACCEPT_SCORE = 0.63

# Per-newspaper tuning.  Gujarat Samachar's thresholds are raised because the
# high-quality embedded templates score 0.90+ on true notices while page
# furniture (disclaimer boxes, obituaries, photo ads) was sneaking in at
# 0.58-0.60.  Sandesh's template was cropped from a small screenshot, so its
# thresholds stay slightly lower.
GS_DETECTION_CONFIG = DetectionConfig(
    box_match_threshold=0.66,
    page_match_threshold=0.70,
)
SANDESH_DETECTION_CONFIG = DetectionConfig(
    box_match_threshold=0.66,
    page_match_threshold=0.72,
)
# Divya Bhaskar ships its own real header samples (db- templates, cropped
# from an actual DB Ahmedabad e-paper PDF at working scale).  DB notices are
# small classifieds whose pill headers sit at ~28-32 px working height, so
# the match scales are DENSE around that size; with the native template the
# pills score 0.74-1.0 while page furniture stays below 0.68.
_DB_STRIP_SCALES = (14, 16, 18, 20, 22, 24, 26, 28, 30, 33, 36, 40, 44,
                    48, 53, 58, 62)
_DB_PAGE_SCALES = (20, 26, 28, 30, 33, 44)
DB_DETECTION_CONFIG = DetectionConfig(
    box_match_threshold=0.68,
    page_match_threshold=0.72,
    strip_scales=_DB_STRIP_SCALES,
    page_scan_scales=_DB_PAGE_SCALES,
)
# Local PDFs can be from any paper: all embedded samples are loaded.
PDF_DETECTION_CONFIG = DetectionConfig(
    box_match_threshold=0.68,
    page_match_threshold=0.72,
    strip_scales=_DB_STRIP_SCALES,
    page_scan_scales=_DB_PAGE_SCALES,
)

# Header title variants.  The e-paper uses a few spellings; English appears
# occasionally in legal notices.  Each entry: (text, script) where script is
# "guj" or "eng" so the right fonts get used.
HEADER_VARIANTS: Tuple[Tuple[str, str], ...] = (
    ("જાહેર નોટિસ", "guj"),
    ("જાહેર નોટીસ", "guj"),
    ("જાહેરનોટિસ", "guj"),
    ("જાહેર નોટીશ", "guj"),
    ("જાહેર ચેતવણી", "guj"),
)

# Real "જાહેર નોટિસ" headers cropped from an actual Gujarat Samachar page and
# embedded as base64 PNGs (grayscale, dark-text-on-white, height 48 px).
# These are the PRIMARY matching templates: they use the newspaper's own
# typeface and need no fonts, no OCR and no external programs.
# (Filled in below - see EMBEDDED_HEADER_TEMPLATES_B64.)

# Visual-order fallbacks used ONLY when Pillow lacks the raqm shaping engine.
# Without complex-script shaping the pre-base vowel sign િ (U+0ABF) is drawn
# after its consonant instead of before it, so we additionally render strings
# with િ manually moved into visual order to keep template matching usable.
HEADER_VARIANTS_NO_RAQM: Tuple[Tuple[str, str], ...] = (
    ("જાહેર નોિટસ", "guj"),
    ("જાહેરનોિટસ", "guj"),
)

# Font-rendered NEGATIVE templates (tender / auction / possession headers).
# Used only to veto candidates whose header looks more like one of these
# than like જાહેર નોટિસ; they can never create a detection on their own.
NEGATIVE_HEADER_VARIANTS: Tuple[Tuple[str, str], ...] = (
    ("જાહેર નિવિદા", "guj"),
    ("ઈ-ટેન્ડર નોટિસ", "guj"),
    ("ટેન્ડર નોટિસ", "guj"),
    ("જાહેર હરાજી", "guj"),
    ("ઈ-હરાજી વેચાણ નોટિસ", "guj"),
    ("કબજા નોટિસ", "guj"),
    ("વેચાણ નોટિસ", "guj"),
)

# ###EMBEDDED_TEMPLATES_START###
EMBEDDED_HEADER_TEMPLATES_B64: Tuple[Tuple[str, str], ...] = (
    ("gs-header-sample-1",
     "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"
     "IDcw/r/0fwGyEqbVOv1M0AAAAABJRU5ErkJggg=="),
    ("gs-header-sample-2",
     "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"),
    ("sandesh-header-sample-1",
     "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"
     "Wd49WIba+PnPXh97CO8v359frzJ7+urT11NQAWalIITKVbH+5osvvloB7Z//1V+9Kv/3f/7lYvDi"
     "aUuFu+1yU7Qfv3w6FHEMoVuu69qmo8UXb778ZkXd6eF0PBl7pmGwrMg0ONyubq9u1/sYVs11bNu2"
     "dJVs1750255NVZ74YW72Rh0j3e+TinXb86x8cbOFNz44GnebjkqTkkS136wXd3c72Tl7/fywgXB5"
     "fXE532TW7MWnrw5BEvyADKCq0uXXv/7ymxtF09d/8efPq3/+r7+6az561LWR+bfzwDl8ejbSy1LT"
     "Tct1bNuu69WHf/n1V7eqORwPBsOx51q2kEozDaSRv7q9X+38UlhOrV53TK0K18t9YXsNEzJPg7Cw"
     "usOOmQd+kkthey2nWt9vldMZDPuthq3yHLYt8tBf3V0vk9rhs7Npg9Lt3c3N3TY1Jk8/eXFgkgKY"
     "QQJQVb785ssvv7nN3NmrP/vTp/n/+W+/urImx6Oep8dX7+65f3IysMEaWHcbzWazZfL8qzdfXaeW"
     "1251BuOu12iYRLqpo8gTf73e+WEO3arVXVNHtlss1oler+lQRRZEpdUddq0yCuIkZ7PVrXOw3hXC"
     "bnjNZt1Gpeym56AIN/MP13sxOD4ZNQwZrReL5TbRBmcvn01MYjDAEEChqu33v/nNt3dlffryP/y7"
     "0+yL//63F6o7mx3PWtnl1+dxYzzpuYaGPJW17mDY79bk8u1X314GbNhuszce9no919AsQ0BWRRKG"
     "UZyWLHTHsQSqeHV3uwxgOwIsszAu7e6w68gkDMO4NNr9lpb4+6RgYTluzRXQvX7PM1W6mZ9fLvLG"
     "eNL3HFGGu812G1H39OnjkUmMB8wMWRKH51/99oelbEyefv7ZLH3zq384z+vDg6dPRtXF777b6K2u"
     "V3cNFezz2uDgYNKv8e763dsP21RBd9qj8XA08izT1sHMqizyoigkQ5impmQRLm5vF3upm0RQeRxX"
     "dnvQcZDFoR/kervfNqs4COKkYN12HUNze8NRx9Xy/fzi/DY0Wt2OV7Moj4NgH7E3Oz3q68T4iBXL"
     "SlB6++7thx3X+0fPnvTS7794c52Y7emTpxN18+33y8qqufW6pfxt5g4ODqZdF/FyfjXfJnnFer0/"
     "GIxGnm3aAkx4oJSUUjFpOskqj5aLxWpfCh1EXKZpZXrtlk1lFgdBpnndlsV5FPpBXMBwbMOs9YeD"
     "bt2Q4eL66m7PTqNRrzu6yrMkjJU7GI9aGjE+YlZKClFs7ubLCE6rPx03s5u371eZXu8fHvawurxa"
     "Z2QYbs1CFOR2dzgeeDYyf71ahVlZSmF57U63W7cMk5iJ8HusFDQBJctkt93uw4o0kECVZ1J36zVL"
     "yDKLk0LUmg0DZRYFUZxDN23dcFvddrNmqGy3XKxDZdiO41g6qjJPM2l5nVZNIwUQwMyKiWTs78Ic"
     "pltrNaxit1xHUrcb7XaNo+3GTyUL07GQZ5XVaLebrokiCYMol0oqaI5br9dtQ9OIFRHhR8wKgkix"
     "KpIoijMpBISAKgpJpmXpglVV5FJYjqWxLNIkzSvohqFrZs11HVPnMgn9KINmGoahCShZFqXSnZpr"
     "EUmAADCDibgs8lKSpum6DpmluRS6YZi6UFWRpWleKt00NClhOI5rGwIyz/MSQkApaIZhGroQxA+I"
     "QAD4AYhAIFUWRVGxECQEuKoqJk0TRMQsWWi6RkpWRVlIJTRdF0LTdU0TAqoqilKRpgkBYiiWUimh"
     "G7pGJAEQwGACEX4iSylBQtN0AqRUJLhI4rQSuqkRSDcNQxfELKUizdDAUjIJEkQAKwaICASAGQAR"
     "PlKSQUIQHkgpFQAhNCIC4SMlq0oqFpqmCQIxA0QAGBBEYKUYADOYAQJIAiCAARAIP+GiVKTpusBH"
     "JQuNijhOS9JNjUjouqFrAmClWOgCD1iBCASAFQP0AB8xA0T4PSb8kVSKSQgN/59KSsVC04QggBUz"
     "fYSfKMZHDP4I/xdEAwkjFuDRMwAAAABJRU5ErkJggg=="),
    ("db-header-sample-1",
     "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"
     "TkSuQmCC"),
    ("db-header-sample-2",
     "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"),
    ("NEG:db-sudharo-1",
     "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"),
)
# ###EMBEDDED_TEMPLATES_END###

# Candidate font files able to render Gujarati, in priority order (bold first:
# notice headers are bold).  Windows ships Nirmala UI and Shruti by default.
GUJARATI_FONT_CANDIDATES: Tuple[str, ...] = (
    r"C:\Windows\Fonts\NirmalaB.ttf",
    r"C:\Windows\Fonts\Nirmala.ttf",
    r"C:\Windows\Fonts\shrutib.ttf",
    r"C:\Windows\Fonts\shruti.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansGujarati-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansGujarati-Regular.ttf",
    "/usr/share/fonts/truetype/lohit-gujarati/Lohit-Gujarati.ttf",
    "/System/Library/Fonts/Supplemental/GujaratiSangamMN.ttc",
    "/System/Library/Fonts/Supplemental/GujaratiMT.ttc",
)
GUJARATI_FONT_GLOBS: Tuple[str, ...] = (
    "/usr/share/fonts/**/*[Gg]ujarat*.tt*",
    "/usr/share/fonts/**/*[Gg]ujarat*.otf",
)
ENGLISH_FONT_CANDIDATES: Tuple[str, ...] = (
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)

# GUI -------------------------------------------------------------------------
GALLERY_THUMB_WIDTH = 210
GALLERY_MIN_COLUMNS = 1
GALLERY_CARD_GAP = 10        # masonry gutter between cards (px)
# Tk cannot place unlimited widgets/images inside one canvas - past a few
# hundred, cards stop drawing (they appear blank).  Cards are therefore
# rendered in batches; every notice is still kept and saved.
GALLERY_RENDER_BATCH = 100    # (legacy, kept for compatibility)
# Notices per screen.  One screen holds one newspaper section; a bigger
# section is split into numbered parts.  60 x ~400 px keeps every screen far
# under Tk's canvas addressing limit.
GALLERY_PAGE_SIZE = 60
# Tk canvases cannot address beyond ~32767 px; stop well short of it so
# cards never land off-canvas (which is what made them look blank).
GALLERY_MAX_CANVAS_PX = 26000
LOG_MAX_LINES = config.LOG_MAX_LINES
PREVIEW_ZOOM_STEP = 1.25
PREVIEW_MIN_ZOOM = 0.05
PREVIEW_MAX_ZOOM = 8.0
PREVIEW_MAX_RENDER_DIM = 6000  # safety cap for zoomed render size (px)


# =============================================================================
# 3. UTILITIES
# =============================================================================

class ExtractionCancelled(Exception):
    """Raised inside the worker thread when the user presses Cancel."""


class ExtractionError(Exception):
    """A fatal, user-presentable extraction problem."""


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def rect_iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    """Intersection-over-union of two (x, y, w, h) rectangles."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(ax, bx)
    iy = max(ay, by)
    ix2 = min(ax + aw, bx + bw)
    iy2 = min(ay + ah, by + bh)
    iw = max(0, ix2 - ix)
    ih = max(0, iy2 - iy)
    inter = iw * ih
    if inter == 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / float(union)


def rect_containment(inner: Tuple[int, int, int, int],
                     outer: Tuple[int, int, int, int]) -> float:
    """Fraction of `inner`'s area that lies inside `outer`."""
    ax, ay, aw, ah = inner
    bx, by, bw, bh = outer
    ix = max(ax, bx)
    iy = max(ay, by)
    ix2 = min(ax + aw, bx + bw)
    iy2 = min(ay + ah, by + bh)
    iw = max(0, ix2 - ix)
    ih = max(0, iy2 - iy)
    if aw * ah == 0:
        return 0.0
    return (iw * ih) / float(aw * ah)


def point_in_rect(px: int, py: int, rect: Tuple[int, int, int, int]) -> bool:
    x, y, w, h = rect
    return x <= px <= x + w and y <= py <= y + h


def bgr_to_pil(img_bgr: "np.ndarray") -> "Image.Image":
    """Convert an OpenCV BGR image to a PIL image."""
    return Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))


def save_image_unicode(img_bgr: "np.ndarray", path: str) -> None:
    """Save an image to disk.  Works with non-ASCII paths (cv2.imwrite does
    not handle those reliably on Windows), so we encode in memory first."""
    ext = os.path.splitext(path)[1].lower() or ".png"
    ok, buf = cv2.imencode(ext, img_bgr)
    if not ok:
        raise IOError(f"Could not encode image for {path}")
    with open(path, "wb") as fh:
        fh.write(buf.tobytes())


# --- text matching helpers (OCR output is noisy) -----------------------------
#: normalize_ocr_text(), fuzzy_contains() and FUZZY_MATCH_RATIO live in
#: utils/search.py - the gallery's Find-text needs exactly the same rules, and
#: two copies of "what counts as a match" would drift.  They are imported at
#: the top of this file, so every plugin's `import *` still sees them.


def match_notice_text(text: str, broad: bool) -> Tuple[float, str]:
    """Does OCR text contain a notice header?  Returns (confidence 0..1,
    matched keyword) - (0.0, "") when it does not."""
    normalized = normalize_ocr_text(text)
    if not normalized:
        return 0.0, ""
    if broad:
        keywords = active_strict_keywords() + BROAD_KEYWORDS[
            len(STRICT_KEYWORDS):]
    else:
        keywords = active_strict_keywords()
    best_ratio, best_kw = 0.0, ""
    for keyword in keywords:
        ratio = fuzzy_contains(normalized, normalize_ocr_text(keyword))
        if ratio > best_ratio:
            best_ratio, best_kw = ratio, keyword
    return best_ratio, best_kw


def match_negative_text(text: str) -> Tuple[float, str]:
    """Does OCR text contain a tender / auction / recruitment header?
    Returns (confidence 0..1, matched keyword) - (0.0, "") when clean."""
    normalized = normalize_ocr_text(text)
    if not normalized:
        return 0.0, ""
    best_ratio, best_kw = 0.0, ""
    for keyword in NEGATIVE_KEYWORDS:
        ratio = fuzzy_contains(normalized, normalize_ocr_text(keyword),
                               NEGATIVE_FUZZY_RATIO)
        if ratio > best_ratio:
            best_ratio, best_kw = ratio, keyword
    return best_ratio, best_kw


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n / 1.0:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"


# --- dependency installer ----------------------------------------------------

def all_pip_packages() -> List[str]:
    """Every pip package the app can use, de-duplicated, in install order."""
    seen: set = set()
    packages: List[str] = []
    for pkg in (list(DEPENDENCY_PACKAGES) + list(BROWSER_PACKAGES)
                + list(OPTIONAL_PACKAGES)
                + list(OPTIONAL_AUTOLOGIN_PACKAGES)
                + list(OCR_PIP_PACKAGES)):
        if pkg and pkg not in seen:
            seen.add(pkg)
            packages.append(pkg)
    return packages


def _run_streamed(command: List[str],
                  line_callback: Callable[[str], None]) -> int:
    """Run `command`, streaming each output line to line_callback."""
    line_callback("> " + " ".join(command))
    creation_flags = 0x08000000 if sys.platform.startswith("win") else 0
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, errors="replace",
            creationflags=creation_flags)
        for line in process.stdout:
            line = line.rstrip()
            if line:
                line_callback(line)
        process.wait()
        return process.returncode or 0
    except FileNotFoundError:
        line_callback(f"  '{command[0]}' not found on this system.")
        return 1
    except Exception as exc:
        line_callback(f"  error: {exc}")
        return 1


def ensure_gujarati_traineddata(
        line_callback: Callable[[str], None]) -> bool:
    """Make sure guj.traineddata exists in the app's own tessdata folder.

    Deliberately not Program Files: writing there needs administrator rights,
    and the engine points TESSDATA_PREFIX at this folder instead."""
    folder = local_tessdata_dir()
    target = os.path.join(folder, "guj.traineddata")
    if os.path.isfile(target) and os.path.getsize(target) > 100_000:
        line_callback(f"  Gujarati language data already present: {target}")
        return True
    line_callback(f"  downloading guj.traineddata -> {folder}")
    try:
        os.makedirs(folder, exist_ok=True)
        opener = urllib.request.build_opener(build_proxy_handler())
        request = urllib.request.Request(
            GUJ_TRAINEDDATA_URL, headers={"User-Agent": USER_AGENT})
        with opener.open(request, timeout=180) as response:
            data = response.read()
        if len(data) < 100_000:
            line_callback(f"  download looks wrong ({len(data)} bytes)")
            return False
        # Write then rename: an interrupted download must never leave a
        # half-written model that Tesseract would choke on.
        temporary = target + ".part"
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, target)
        line_callback(f"  saved {len(data):,} bytes")
        return True
    except Exception as exc:
        line_callback(f"  could not download Gujarati data: {exc}")
        line_callback(f"  get it manually from {GUJ_TRAINEDDATA_URL}")
        line_callback(f"  and save it as {target}")
        return False


def ensure_tesseract(line_callback: Callable[[str], None]) -> bool:
    """Install the Tesseract PROGRAM if it is missing.

    Installs onto the app's own drive (see preferred_tesseract_dir), not the
    system drive.  Returns True when a usable tesseract.exe exists."""
    existing = TesseractOcrEngine.find_binary()
    if existing:
        line_callback(f"  Tesseract already installed: {existing}")
        return True
    if not sys.platform.startswith("win"):
        line_callback("  install Tesseract with your package manager, e.g. "
                      "'sudo apt install tesseract-ocr tesseract-ocr-guj'")
        return False
    if shutil.which("winget") is None:
        line_callback("  winget is not available - install Tesseract from "
                      "https://github.com/UB-Mannheim/tesseract/wiki")
        return False
    target = preferred_tesseract_dir()
    line_callback(f"  installing Tesseract into {target} (a few minutes)...")
    code = _run_streamed(
        ["winget", "install", "--id", "UB-Mannheim.TesseractOCR", "-e",
         "--silent", "--accept-package-agreements",
         "--accept-source-agreements", "--location", target],
        line_callback)
    if code != 0 or TesseractOcrEngine.find_binary() is None:
        # --location is honoured only by installers that support it; fall
        # back to a default-location install rather than leaving none at all.
        line_callback("  retrying with the installer's default location...")
        _run_streamed(
            ["winget", "install", "--id", "UB-Mannheim.TesseractOCR", "-e",
             "--silent", "--accept-package-agreements",
             "--accept-source-agreements"],
            line_callback)
    found = TesseractOcrEngine.find_binary()
    if found:
        line_callback(f"  Tesseract ready: {found}")
        return True
    line_callback("  Tesseract install did not complete - install it "
                  "manually from https://github.com/UB-Mannheim/tesseract")
    return False


def ensure_browser(line_callback: Callable[[str], None]) -> bool:
    """Make sure a driveable browser exists for the automated session.

    Chromium is only downloaded when the machine has no Chrome/Edge that can
    be driven directly - most Windows machines skip the 120 MB."""
    try:
        from .scrapers.browser_session import (browser_ready,
                                               default_browser_channel,
                                               install_browser)
    except ImportError as exc:
        line_callback(f"  browser automation unavailable: {exc}")
        return False
    if browser_ready():
        channel = default_browser_channel()
        line_callback("  browser ready: "
                      + (f"the system {channel}" if channel
                         else "Playwright Chromium"))
        return True
    line_callback("  downloading Chromium for Playwright (~120 MB)...")
    return install_browser(lambda text, level="info": line_callback(text))


def pip_install_dependencies(line_callback: Callable[[str], None],
                             done_callback: Callable[[int], None]) -> None:
    """Install EVERYTHING the app can use, then call done_callback(rc).

    Three stages, because "dependencies" here is not only pip packages:
      1. pip packages          (opencv, pillow, pymupdf, winrt, pytesseract)
      2. the Tesseract program (external, installed onto the app's drive)
      3. Gujarati language data (guj.traineddata)

    Runs in the calling thread - callers wrap it in a worker thread."""
    failures: List[str] = []
    try:
        line_callback("[1/4] Python packages")
        code = _run_streamed(
            [sys.executable, "-m", "pip", "install", "--upgrade",
             *all_pip_packages()], line_callback)
        if code != 0:
            # One bad package must not stop the OCR program from installing,
            # so this is recorded and the run continues.
            failures.append("pip packages")

        line_callback("")
        line_callback("[2/4] Automation browser (Divya Bhaskar session)")
        if not ensure_browser(line_callback):
            failures.append("automation browser")

        line_callback("")
        line_callback("[3/4] Tesseract OCR program")
        if not ensure_tesseract(line_callback):
            failures.append("Tesseract program")

        line_callback("")
        line_callback("[4/4] Gujarati language data")
        if not ensure_gujarati_traineddata(line_callback):
            failures.append("Gujarati traineddata")

        line_callback("")
        reset_ocr_engine_cache()          # re-probe with what now exists
        for line in validate_ocr_setup()[0]:
            line_callback("  " + line)
        if failures:
            line_callback("")
            line_callback("Incomplete: " + ", ".join(failures))
        done_callback(1 if failures else 0)
    except Exception as exc:
        line_callback(f"Installer error: {exc}")
        done_callback(1)


def restart_application() -> None:
    """Relaunch this script with the same interpreter and arguments."""
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception:
        sys.exit(0)   # at worst, just quit; the user restarts manually


def find_gujarati_ui_font(root: "tk.Tk") -> str:
    """Pick an installed font family that renders Gujarati in Tk widgets
    (native message boxes cannot be font-controlled, so custom dialogs use
    this)."""
    try:
        families = {f.lower() for f in tkfont.families(root)}
    except Exception:
        families = set()
    for name in ("Nirmala UI", "Shruti", "Arial Unicode MS",
                 "Noto Sans Gujarati", "Lohit Gujarati"):
        if name.lower() in families:
            return name
    return "Segoe UI" if sys.platform.startswith("win") else "TkDefaultFont"


# =============================================================================
# 4. PROGRESS REPORTING / WORKER PLUMBING
# =============================================================================

@dataclass
class NoticeResult:
    """One detected & cropped Public Notice."""
    result_id: int
    page_number: int
    index_on_page: int
    image_bgr: "np.ndarray"
    confidence: int              # 0-100
    method: str                  # "box+header", "page-scan", ...
    edition: str = ""            # e.g. "ahmedabad-east" / "GS-ahmedabad"
    newspaper: str = ""          # e.g. "Divya Bhaskar"
    issue_date: str = ""         # ISO date of the edition
    #: gallery section this notice belongs to.  Set by the job runner so that
    #: notices streaming in from several editions at once still land under the
    #: right heading instead of whichever section happens to be last.
    section_title: str = ""
    #: Words read out of the crop, filled in lazily the first time the user
    #: searches (OCRing every crop up front would slow every run for a
    #: feature most runs never use).
    ocr_words: List["OcrWord"] = field(default_factory=list)
    ocr_text: str = ""
    #: True once the OCR above has been attempted, successfully or not.
    ocr_done: bool = False
    #: Word boxes matching the CURRENT search, in crop pixel coordinates.
    match_boxes: List[Tuple[int, int, int, int]] = field(default_factory=list)

    @property
    def suggested_filename(self) -> str:
        bits = [b for b in (self.newspaper.replace(" ", ""),
                            self.edition, self.issue_date) if b]
        prefix = ("_".join(bits) + "_") if bits else ""
        return (f"notice_{prefix}page{self.page_number:02d}"
                f"_{self.index_on_page:03d}.png")

    @property
    def caption(self) -> str:
        prefix = f"{self.edition}  ·  " if self.edition else ""
        return (f"{prefix}Page {self.page_number}  ·  #{self.index_on_page}"
                f"  ·  {self.confidence}%")


class ProgressReporter:
    """Thread-safe channel from the worker thread to the GUI.

    The worker calls these methods; each posts a message onto a queue that the
    Tk main loop drains.  `check_cancel()` raises ExtractionCancelled when the
    user pressed Cancel, which unwinds the worker cleanly.
    """

    def __init__(self, msg_queue: "queue.Queue", cancel_event: threading.Event):
        self._q = msg_queue
        self._cancel = cancel_event

    # -- worker-side API ------------------------------------------------------
    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise ExtractionCancelled()

    def log(self, text: str, level: str = "info") -> None:
        self._q.put(("log", text, level))

    def separator(self) -> None:
        self._q.put(("log", "-" * 46, "dim"))

    def phase(self, text: str) -> None:
        self._q.put(("phase", text))

    def progress(self, current: int, total: int) -> None:
        self._q.put(("progress", current, total))

    def result(self, res: NoticeResult) -> None:
        self._q.put(("result", res))

    def heading(self, text: str) -> None:
        """A section title for the gallery (one per newspaper/edition/date)."""
        self._q.put(("heading", text))

    def done(self, summary: str) -> None:
        self._q.put(("done", summary))

    def failed(self, message: str) -> None:
        self._q.put(("failed", message))

    def cancelled(self) -> None:
        self._q.put(("cancelled",))


# =============================================================================
# 5. DOWNLOADER
# =============================================================================

class PageDownloader:
    """HTTP download helper with retries, a session on-disk cache and an
    in-memory decoded-image cache so no URL is fetched twice."""

    def __init__(self, reporter: ProgressReporter):
        self._reporter = reporter
        self._cache_dir = tempfile.mkdtemp(prefix="pne_cache_")
        self._image_cache: Dict[str, "np.ndarray"] = {}
        # A cookie jar carried across the whole session: seeded from any
        # stored login cookie and auto-updated from Set-Cookie responses, so
        # a token the site refreshes mid-visit is reused for later requests.
        self._cookie_jar = http.cookiejar.CookieJar()
        self._lock = threading.Lock()          # guards the caches below
        self._opener = urllib.request.build_opener(
            build_proxy_handler(),
            urllib.request.HTTPCookieProcessor(self._cookie_jar))
        proxy = load_proxy(force=True)
        if proxy:
            reporter.log(f"Using proxy: {proxy}", "dim")
        atexit.register(self.cleanup)

    def seed_cookies(self, cookie_header: str,
                     domains: Tuple[str, ...]) -> None:
        """Seed the jar from a raw 'k=v; k2=v2' cookie string for the given
        domains (so it is sent to the site AND its image CDN)."""
        if not cookie_header:
            return
        for pair in cookie_header.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            for domain in domains:
                self._cookie_jar.set_cookie(http.cookiejar.Cookie(
                    version=0, name=name.strip(), value=value.strip(),
                    port=None, port_specified=False, domain=domain,
                    domain_specified=True, domain_initial_dot=domain.startswith("."),
                    path="/", path_specified=True, secure=True, expires=None,
                    discard=False, comment=None, comment_url=None, rest={}))

    # -- lifecycle ------------------------------------------------------------
    def cleanup(self) -> None:
        try:
            shutil.rmtree(self._cache_dir, ignore_errors=True)
        except Exception:
            pass

    # -- low level ------------------------------------------------------------
    def _cache_path(self, url: str) -> str:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return os.path.join(self._cache_dir, digest)

    def fetch_bytes(self, url: str, *, referer: Optional[str] = None,
                    extra_headers: Optional[Dict[str, str]] = None) -> bytes:
        """Download raw bytes with retries; uses the on-disk session cache."""
        cache_file = self._cache_path(url)
        if os.path.exists(cache_file):
            with open(cache_file, "rb") as fh:
                return fh.read()

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9,gu;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        if extra_headers:
            headers.update(extra_headers)

        last_error: Optional[Exception] = None
        for attempt in range(1 + HTTP_RETRIES):
            self._reporter.check_cancel()
            try:
                request = urllib.request.Request(url, headers=headers)
                with self._opener.open(request,
                                       timeout=HTTP_TIMEOUT_SECONDS) as resp:
                    # Read in chunks so a user's Cancel takes effect
                    # immediately, even mid-download of a large page image.
                    parts: List[bytes] = []
                    while True:
                        self._reporter.check_cancel()
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        parts.append(chunk)
                    data = b"".join(parts)
                with open(cache_file, "wb") as fh:
                    fh.write(data)
                return data
            except ExtractionCancelled:
                raise
            except (urllib.error.HTTPError, urllib.error.URLError,
                    TimeoutError, ConnectionError, OSError) as exc:
                last_error = exc
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                    break  # retrying a 404 is pointless
                if attempt < HTTP_RETRIES:
                    # Sleep in small steps so Cancel is not delayed.
                    slept = 0.0
                    while slept < HTTP_RETRY_DELAY_SECONDS:
                        self._reporter.check_cancel()
                        time.sleep(0.1)
                        slept += 0.1
        raise ExtractionError(f"Download failed: {url} ({last_error})")

    def fetch_text(self, url: str, *, referer: Optional[str] = None,
                   extra_headers: Optional[Dict[str, str]] = None) -> str:
        data = self.fetch_bytes(url, referer=referer,
                                extra_headers=extra_headers)
        return data.decode("utf-8", errors="replace")

    def fetch_image(self, url: str, *, referer: Optional[str] = None,
                    extra_headers: Optional[Dict[str, str]] = None
                    ) -> "np.ndarray":
        """Download and decode an image (BGR).  Cached per session.
        Safe to call from several threads at once."""
        with self._lock:
            cached = self._image_cache.get(url)
        if cached is not None:
            return cached
        data = self.fetch_bytes(url, referer=referer,
                                extra_headers=extra_headers)
        array = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if img is None:
            raise ExtractionError(f"Could not decode image: {url}")
        with self._lock:
            self._image_cache[url] = img
        return img


@dataclass
class PageRef:
    """A reference to one page of an edition.

    Shared by every newspaper plugin: discover() returns a list of these and
    fetch_page() turns one into an image."""
    page_number: int
    image_url: Optional[str]      # best-known full-resolution URL (may be None)
    thumb_url: Optional[str]
    page_html_url: str            # the page's own HTML URL (fallback resolver)


# ---------------------------------------------------------------
# PDF page sources.  Shared: the local-PDF plugin, Divya Bhaskar
# (its editions are PDFs) and Nav Gujarat Samay all render pages
# through these, so they belong here rather than in any one plugin.
# ---------------------------------------------------------------

PDF_MAX_PAGES = 60
PDF_RENDER_WIDTH = 1700       # px width every page is rendered at

def pdf_path_from_url(url: str) -> Optional[str]:
    """Accepts a plain file path or file:/// URL to a .pdf; returns the
    filesystem path, or None when it is not an existing PDF file."""
    candidate = url.strip().strip('"').strip("'")
    low = candidate.lower()
    if low.startswith("file:///"):
        candidate = urllib.request.url2pathname(candidate[8:])
    elif low.startswith("file://"):
        candidate = candidate[7:]
    if not candidate.lower().endswith(".pdf"):
        return None
    return candidate if os.path.isfile(candidate) else None


def pdf_discover_pages(url: str,
                       reporter: ProgressReporter) -> List[PageRef]:
    path = pdf_path_from_url(url)
    if path is None:
        raise ExtractionError("The PDF file could not be found:\n" + url)
    if not _HAVE_FITZ:
        raise ExtractionError(
            "Reading PDF files needs the PyMuPDF package.\n"
            "Press 'Download Dependencies' (it installs pymupdf), restart "
            "the application and try again.")
    reporter.log(f"Opening PDF: {os.path.basename(path)}")
    try:
        document = fitz.open(path)
    except Exception as exc:
        raise ExtractionError(f"Could not open the PDF:\n{exc}")
    try:
        total = min(document.page_count, PDF_MAX_PAGES)
    finally:
        document.close()
    if total < 1:
        raise ExtractionError("The PDF contains no pages.")
    pages = [PageRef(page_number=number, image_url=None, thumb_url=None,
                     page_html_url=path)
             for number in range(1, total + 1)]
    reporter.log(f"PDF has {total} pages.")
    return pages


def pdf_render_page(path: str, page_number: int) -> "np.ndarray":
    """Render one PDF page to a BGR image at print-quality width."""
    document = fitz.open(path)
    try:
        page = document[page_number - 1]
        zoom = PDF_RENDER_WIDTH / max(1.0, float(page.rect.width))
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
            pixmap.height, pixmap.width, pixmap.n)
        if pixmap.n == 4:
            return cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
        if pixmap.n == 3:
            return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    finally:
        document.close()


def pdf_pages_from_web(downloader: PageDownloader, pdf_url: str,
                       reporter: ProgressReporter) -> List[PageRef]:
    """Download an e-paper PDF from the web and expose its pages exactly
    like a local PDF file (used by Divya Bhaskar and pasted .pdf links)."""
    if not _HAVE_FITZ:
        raise ExtractionError(
            "Reading PDF files needs the PyMuPDF package.\n"
            "Press 'Download Dependencies' (it installs pymupdf), restart "
            "the application and try again.")
    reporter.log(f"Downloading PDF: {pdf_url}")
    extra_headers = None
    try:
        if "bhaskar" in urllib.parse.urlsplit(pdf_url).netloc.lower():
            extra_headers = newspaper_module(
                "divya_bhaskar")._db_headers()
    except ValueError:
        pass
    data = downloader.fetch_bytes(pdf_url, referer=pdf_url,
                                  extra_headers=extra_headers)
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise ExtractionError(
            "The link did not return a PDF file:\n" + pdf_url)
    local_path = downloader._cache_path(pdf_url) + ".pdf"
    with open(local_path, "wb") as fh:
        fh.write(data)
    try:
        document = fitz.open(local_path)
    except Exception as exc:
        raise ExtractionError(f"Could not open the downloaded PDF:\n{exc}")
    try:
        total = min(document.page_count, PDF_MAX_PAGES)
    finally:
        document.close()
    if total < 1:
        raise ExtractionError("The downloaded PDF contains no pages.")
    reporter.log(f"PDF has {total} pages.")
    return [PageRef(page_number=number, image_url=None, thumb_url=None,
                    page_html_url=local_path)
            for number in range(1, total + 1)]


# =============================================================================
# 6.5  OCR ENGINES (Gujarati)  -  Tesseract first, Windows OCR fallback
# =============================================================================

@dataclass
class OcrWord:
    """One recognized word with its bounding box (image coordinates)."""
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float          # 0..1


class BaseOcrEngine:
    """Minimal OCR interface the detection pipeline relies on."""

    name = "none"
    supports_gujarati = False

    def read_text(self, gray: "np.ndarray",
                  early_stop: Optional[Callable[[str], bool]] = None) -> str:
        """OCR a small region (header strip) and return raw text.

        `early_stop(text_so_far)` returning True lets a multi-pass engine
        skip its remaining (slower) passes once the answer is already in."""
        raise NotImplementedError

    def read_words(self, gray: "np.ndarray") -> List[OcrWord]:
        """OCR a full page and return positioned words (used by the
        full-page sweep on pages where nothing else was found)."""
        raise NotImplementedError


class TesseractOcrEngine(BaseOcrEngine):
    """pytesseract wrapper.  Auto-locates tesseract.exe in the usual install
    directories, prefers the 'guj' language pack."""

    name = "Tesseract"

    def __init__(self, lang: str, supports_gujarati: bool):
        self.lang = lang
        self.supports_gujarati = supports_gujarati

    @staticmethod
    def find_binary() -> Optional[str]:
        """tesseract.exe, preferring a copy on the app's own drive.

        The known locations are checked BEFORE PATH: a stale system-drive
        install left on PATH must not shadow the local one this app manages."""
        for candidate in _tesseract_candidates():
            if candidate and os.path.isfile(candidate):
                return candidate
        return shutil.which("tesseract")

    @staticmethod
    def use_local_tessdata() -> Optional[str]:
        """Point Tesseract at the app-local 'tessdata' folder when it holds a
        Gujarati model.

        Dropping guj.traineddata next to the program means Gujarati OCR needs
        no administrator rights - writing into
        'C:\\Program Files\\Tesseract-OCR\\tessdata' does."""
        folder = local_tessdata_dir()
        if not os.path.isfile(os.path.join(folder, "guj.traineddata")):
            return None
        # Redirecting TESSDATA_PREFIX must not LOSE languages: with only guj
        # in the local folder Tesseract would drop English entirely (search
        # and guj+eng OCR both need it).  Best case: the install's own
        # tessdata is writable (an E:\ install is) - copy guj THERE and skip
        # the redirect.  Otherwise copy eng/osd into the local folder so the
        # redirected dir carries every language the install had.
        binary = TesseractOcrEngine.find_binary()
        install = os.path.join(os.path.dirname(binary or ""), "tessdata")
        if binary and os.path.isdir(install):
            try:
                shutil.copy2(os.path.join(folder, "guj.traineddata"),
                             os.path.join(install, "guj.traineddata"))
                os.environ.pop("TESSDATA_PREFIX", None)
                return install                # install dir now has everything
            except OSError:
                pass                          # not writable (Program Files)
            for lang_file in ("eng.traineddata", "osd.traineddata"):
                source = os.path.join(install, lang_file)
                target = os.path.join(folder, lang_file)
                if os.path.isfile(source) and not os.path.isfile(target):
                    try:
                        shutil.copy2(source, target)
                    except OSError:
                        pass
        # Tesseract 5 expects TESSDATA_PREFIX to be the tessdata dir itself.
        # The env var (not --tessdata-dir) is deliberate: pytesseract splits
        # its config string on whitespace, so a path containing a space -
        # which this app's own folder may well have - would be torn in two.
        os.environ["TESSDATA_PREFIX"] = folder
        return folder

    @classmethod
    def create(cls) -> Optional["TesseractOcrEngine"]:
        if not _HAVE_PYTESSERACT:
            return None
        binary = cls.find_binary()
        if not binary:
            return None
        pytesseract.pytesseract.tesseract_cmd = binary
        try:
            languages = set(pytesseract.get_languages(config=""))
        except Exception:
            return None
        if "guj" not in languages and cls.use_local_tessdata():
            try:
                languages = set(pytesseract.get_languages(config=""))
            except Exception:
                return None
        if "guj" in languages:
            lang = "guj+eng" if "eng" in languages else "guj"
            return cls(lang, True)
        if "eng" in languages:
            return cls("eng", False)
        return None

    def read_text(self, gray: "np.ndarray",
                  early_stop: Optional[Callable[[str], bool]] = None) -> str:
        # A header strip mixes one display-size title line with dense body
        # text.  psm 6 (uniform block) often drops the isolated big line and
        # psm 11 (sparse) sometimes mangles the dense part - run both and
        # let the keyword matcher search the combined output.  Each pass is
        # one tesseract.exe subprocess, so when the first already contains
        # the keyword the second is skipped (halves the cost of every strip
        # that really is a notice).
        pieces = []
        for psm in (11, 6):
            try:
                pieces.append(pytesseract.image_to_string(
                    gray, lang=self.lang, config=f"--psm {psm}"))
            except Exception:
                continue
            if early_stop is not None and pieces and early_stop(pieces[-1]):
                break
        return "\n".join(pieces)

    def read_words(self, gray: "np.ndarray") -> List[OcrWord]:
        words: List[OcrWord] = []
        try:
            # psm 11: sparse text - finds isolated headlines on a full page.
            data = pytesseract.image_to_data(
                gray, lang=self.lang, config="--psm 11",
                output_type=pytesseract.Output.DICT)
        except Exception:
            return words
        for i in range(len(data.get("text", []))):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                conf = -1.0
            if conf < 20:          # ignore garbage
                continue
            words.append(OcrWord(text, int(data["left"][i]),
                                 int(data["top"][i]), int(data["width"][i]),
                                 int(data["height"][i]), conf / 100.0))
        return words


class WindowsOcrEngine(BaseOcrEngine):
    """Windows built-in OCR (Windows.Media.Ocr) through the winsdk / winrt
    Python bindings.  Requires the Gujarati language pack to be installed in
    Windows Settings for Gujarati recognition."""

    name = "Windows OCR"

    def __init__(self, winsdk_modules: tuple, engine, supports_gujarati: bool):
        self._mod = winsdk_modules
        self._engine = engine
        self.supports_gujarati = supports_gujarati
        try:
            self._max_dim = int(type(engine).max_image_dimension)
        except Exception:
            self._max_dim = 2600

    # -- construction ---------------------------------------------------------
    @classmethod
    def create(cls) -> Optional["WindowsOcrEngine"]:
        if not sys.platform.startswith("win"):
            return None
        modules = cls._import_winsdk()
        if modules is None:
            return None
        ocr_mod, lang_cls, imaging, streams = modules
        engine = None
        supports_guj = False
        try:
            if ocr_mod.OcrEngine.is_language_supported(lang_cls("gu")):
                engine = ocr_mod.OcrEngine.try_create_from_language(
                    lang_cls("gu"))
                supports_guj = engine is not None
        except Exception:
            engine = None
        if engine is None:
            try:
                engine = \
                    ocr_mod.OcrEngine.try_create_from_user_profile_languages()
                if engine is not None:
                    tag = str(engine.recognizer_language.language_tag)
                    supports_guj = tag.lower().startswith("gu")
            except Exception:
                engine = None
        if engine is None:
            return None
        return cls(modules, engine, supports_guj)

    @staticmethod
    def _import_winsdk():
        """Try the 'winsdk' package first, then the older 'winrt' package."""
        for root in ("winsdk", "winrt"):
            try:
                import importlib
                ocr_mod = importlib.import_module(
                    f"{root}.windows.media.ocr")
                glob_mod = importlib.import_module(
                    f"{root}.windows.globalization")
                imaging = importlib.import_module(
                    f"{root}.windows.graphics.imaging")
                streams = importlib.import_module(
                    f"{root}.windows.storage.streams")
                return ocr_mod, glob_mod.Language, imaging, streams
            except Exception:
                continue
        return None

    # -- recognition ----------------------------------------------------------
    def _thread_engine(self):
        """A recognizer owned by the calling thread.

        WinRT objects are happiest when they are not shared between
        threads, so each OCR worker builds (and keeps) its own engine and
        falls back to the shared one if that is not possible."""
        local = getattr(self, "_local", None)
        if local is None:
            local = threading.local()
            self._local = local
        engine = getattr(local, "engine", None)
        if engine is None:
            engine = None
            try:
                ocr_mod, lang_cls, _imaging, _streams = self._mod
                if self.supports_gujarati:
                    engine = ocr_mod.OcrEngine.try_create_from_language(
                        lang_cls("gu"))
                if engine is None:
                    engine = (ocr_mod.OcrEngine
                              .try_create_from_user_profile_languages())
            except Exception:
                engine = None
            local.engine = engine or self._engine
        return local.engine

    def _recognize(self, gray: "np.ndarray") -> Tuple[List[OcrWord], float]:
        """Run Windows OCR; returns (words, scale) where scale maps OCR
        coordinates back to the input image.  Safe to call from several
        threads at once."""
        import asyncio

        img = gray
        scale = 1.0
        largest = max(img.shape[:2])
        if largest > self._max_dim:
            scale = self._max_dim / float(largest)
            img = cv2.resize(img, (max(1, int(img.shape[1] * scale)),
                                   max(1, int(img.shape[0] * scale))),
                             interpolation=cv2.INTER_AREA)
        ok, png = cv2.imencode(".png", img)
        if not ok:
            return [], 1.0
        png_bytes = png.tobytes()
        _, _, imaging, streams = self._mod
        engine = self._thread_engine()

        async def run():
            stream = streams.InMemoryRandomAccessStream()
            writer = streams.DataWriter(stream.get_output_stream_at(0))
            writer.write_bytes(png_bytes)
            await writer.store_async()
            await writer.flush_async()
            decoder = await imaging.BitmapDecoder.create_async(stream)
            bitmap = await decoder.get_software_bitmap_async()
            return await engine.recognize_async(bitmap)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(run())
        except Exception:
            return [], 1.0
        finally:
            try:
                loop.close()          # one loop per call, always released
            except Exception:
                pass

        words: List[OcrWord] = []
        try:
            for line in result.lines:
                for word in line.words:
                    rect = word.bounding_rect
                    words.append(OcrWord(
                        str(word.text),
                        int(rect.x / scale), int(rect.y / scale),
                        max(1, int(rect.width / scale)),
                        max(1, int(rect.height / scale)),
                        0.70))       # the API exposes no per-word confidence
        except Exception:
            pass
        return words, scale

    def read_text(self, gray: "np.ndarray",
                  early_stop: Optional[Callable[[str], bool]] = None
                  ) -> str:   # single pass - early_stop has no work
        words, _ = self._recognize(gray)
        return " ".join(w.text for w in words)

    def read_words(self, gray: "np.ndarray") -> List[OcrWord]:
        words, _ = self._recognize(gray)
        return words


class EasyOcrEngine(BaseOcrEngine):
    """EasyOCR wrapper (pure-pip, no system binary).

    NOTE: as of easyocr 1.7.x the model zoo has NO Gujarati ('gu') model - it
    covers Devanagari but not Gujarati script.  This rung therefore only ever
    activates when a future easyocr build adds 'gu', or when
    OCR_REQUIRE_GUJARATI is switched off.  Kept so the chain lights up
    automatically the day the model lands."""

    name = "EasyOCR"

    def __init__(self, reader, supports_gujarati: bool):
        self._reader = reader
        self.supports_gujarati = supports_gujarati

    @classmethod
    def available_langs(cls) -> Optional[set]:
        """The language codes easyocr can build a reader for, or None when
        easyocr is not installed.  Import only - no model download."""
        try:
            from easyocr.config import all_lang_list      # type: ignore
        except Exception:
            return None
        try:
            return set(all_lang_list)
        except Exception:
            return set()

    @classmethod
    def create(cls) -> Optional["EasyOcrEngine"]:
        langs = cls.available_langs()
        if langs is None:
            return None
        if "gu" in langs:
            wanted, guj = ["gu", "en"], True
        elif "en" in langs:
            wanted, guj = ["en"], False
        else:
            return None
        # Building a Reader downloads ~100 MB of weights on first use, so do
        # not build one we are only going to throw away for lacking Gujarati.
        if not guj and OCR_REQUIRE_GUJARATI:
            return None
        try:
            import easyocr                                # type: ignore
            # gpu=False keeps every edition agent off the same GPU queue.
            reader = easyocr.Reader(wanted, gpu=False, verbose=False)
        except Exception:
            return None
        return cls(reader, guj)

    def _read(self, gray: "np.ndarray") -> List[OcrWord]:
        try:
            raw = self._reader.readtext(gray, detail=1, paragraph=False)
        except Exception:
            return []
        words: List[OcrWord] = []
        for item in raw:
            try:
                box, text, conf = item[0], item[1], float(item[2])
            except (IndexError, TypeError, ValueError):
                continue
            text = (text or "").strip()
            if not text or conf < 0.20:
                continue
            xs = [int(p[0]) for p in box]
            ys = [int(p[1]) for p in box]
            words.append(OcrWord(text, min(xs), min(ys),
                                 max(1, max(xs) - min(xs)),
                                 max(1, max(ys) - min(ys)), conf))
        return words

    def read_text(self, gray: "np.ndarray",
                  early_stop: Optional[Callable[[str], bool]] = None
                  ) -> str:   # single pass - early_stop has no work
        return " ".join(w.text for w in self._read(gray))

    def read_words(self, gray: "np.ndarray") -> List[OcrWord]:
        return self._read(gray)


def validate_ocr_setup() -> Tuple[List[str], List[str]]:
    """Inspect every OCR backend WITHOUT building one.

    Returns (status_lines, fix_lines): what is present, and what the user
    would have to do to light up the rungs that are dark.  Cheap enough to
    run at startup - no model loads, no WinRT engine construction."""
    status: List[str] = []
    fixes: List[str] = []

    # -- 1. Windows OCR --------------------------------------------------------
    if not sys.platform.startswith("win"):
        status.append("Windows OCR : n/a (not Windows)")
    elif not USE_WINDOWS_OCR:
        status.append("Windows OCR : disabled (USE_WINDOWS_OCR = False)")
    elif WindowsOcrEngine._import_winsdk() is None:
        status.append("Windows OCR : winsdk/winrt NOT installed")
        fixes.append("Windows OCR: run  " + WINRT_INSTALL_HINT)
    else:
        try:
            ocr_mod, lang_cls, _i, _s = WindowsOcrEngine._import_winsdk()
            guj = bool(ocr_mod.OcrEngine.is_language_supported(lang_cls("gu")))
            # Listing the installed recognisers needs the separate
            # ...Foundation.Collections package; its absence must not hide the
            # answer we actually care about.
            try:
                langs = ", ".join(
                    str(l.language_tag)
                    for l in ocr_mod.OcrEngine.available_recognizer_languages)
            except Exception:
                langs = "language list unavailable"
            if guj:
                status.append(f"Windows OCR : READY with Gujarati [{langs}]")
            else:
                status.append(f"Windows OCR : installed, NO Gujarati "
                              f"[{langs}]")
                fixes.append(
                    "Windows OCR: add the Gujarati OCR pack.  Fastest way, in "
                    "an ADMIN PowerShell:  "
                    "DISM /Online /Add-Capability "
                    "/CapabilityName:Language.OCR~~~gu-IN~0.0.1.0     "
                    "(or Settings > Time & Language > Language & Region > "
                    "Add a language > ગુજરાતી, then Language options > "
                    "install the optional OCR component)")
        except Exception as exc:
            status.append(f"Windows OCR : probe failed ({exc})")

    # -- 2. Tesseract ----------------------------------------------------------
    if not USE_TESSERACT_OCR:
        status.append("Tesseract   : disabled (USE_TESSERACT_OCR = False)")
    elif not _HAVE_PYTESSERACT:
        status.append("Tesseract   : pytesseract NOT installed")
        fixes.append("Tesseract: run  pip install pytesseract  and install "
                     "the Tesseract program (see below)")
    else:
        binary = TesseractOcrEngine.find_binary()
        if not binary:
            status.append("Tesseract   : program NOT found")
            fixes.append(
                "Tesseract: install it -  winget install "
                "UB-Mannheim.TesseractOCR   "
                "(or https://github.com/UB-Mannheim/tesseract/wiki), then run "
                "setup_ocr.py to add Gujarati")
        else:
            try:
                pytesseract.pytesseract.tesseract_cmd = binary
                langs = set(pytesseract.get_languages(config=""))
                if "guj" not in langs and \
                        TesseractOcrEngine.use_local_tessdata():
                    langs = set(pytesseract.get_languages(config=""))
            except Exception as exc:
                langs = set()
                status.append(f"Tesseract   : found but unusable ({exc})")
            if "guj" in langs:
                status.append(f"Tesseract   : READY with Gujarati [{binary}]")
            elif langs:
                status.append(f"Tesseract   : installed, NO 'guj' data "
                              f"[{binary}]")
                fixes.append(
                    "Tesseract: add Gujarati data - run  python setup_ocr.py  "
                    "(downloads guj.traineddata into '"
                    + local_tessdata_dir() + "', no admin rights needed)")

    # -- 3. EasyOCR ------------------------------------------------------------
    if not USE_EASYOCR:
        status.append("EasyOCR     : disabled (USE_EASYOCR = False)")
    else:
        langs = EasyOcrEngine.available_langs()
        if langs is None:
            status.append("EasyOCR     : NOT installed")
            fixes.append("EasyOCR (last resort): pip install easyocr  "
                         "- note it has no Gujarati model today")
        elif "gu" in langs:
            status.append("EasyOCR     : READY with Gujarati")
        else:
            status.append("EasyOCR     : installed, NO Gujarati model "
                          "(upstream limitation)")
    return status, fixes


# The engine is expensive to build (a Tesseract language probe spawns a
# process, WinRT builds a recognizer, EasyOCR loads torch weights) and every
# edition agent asks for one.  Build it once for the whole run.
_ocr_engine_cache: List[Optional[BaseOcrEngine]] = []
_ocr_engine_lock = threading.Lock()
_ocr_setup_logged = threading.Event()


def reset_ocr_engine_cache() -> None:
    """Forget the cached engine (used after a dependency install)."""
    with _ocr_engine_lock:
        _ocr_engine_cache.clear()
    _ocr_setup_logged.clear()


def _build_ocr_engine(log) -> Optional[BaseOcrEngine]:
    """Walk the backend chain and return the first usable engine."""
    require_guj = OCR_REQUIRE_GUJARATI
    latin_only: Optional[BaseOcrEngine] = None

    rungs = (
        ("Windows OCR", USE_WINDOWS_OCR, WindowsOcrEngine.create),
        ("Tesseract", USE_TESSERACT_OCR, TesseractOcrEngine.create),
        ("EasyOCR", USE_EASYOCR, EasyOcrEngine.create),
    )
    for label, enabled, factory in rungs:
        if not enabled:
            continue
        try:
            engine = factory()
        except Exception as exc:
            log(f"[OCR] {label} probe failed: {exc}", "dim")
            continue
        if engine is None:
            log(f"[OCR] {label} not usable -> trying next backend", "dim")
            continue
        if engine.supports_gujarati:
            log(f"[OCR] Using {engine.name} (Gujarati)", "success")
            return engine
        log(f"[OCR] {label} available but WITHOUT Gujarati -> "
            "trying next backend", "warn")
        latin_only = latin_only or engine

    if latin_only is not None and not require_guj:
        log(f"[OCR] Falling back to {latin_only.name} (Latin only - "
            "Gujarati headers will NOT be matched)", "warn")
        return latin_only
    return None


def select_ocr_engine(reporter: ProgressReporter) -> Optional[BaseOcrEngine]:
    """The OCR engine for this run, built once and shared by every agent.

    Backend priority (each rung can be switched off by a USE_* flag):
        1. Windows built-in OCR  - winsdk + the Gujarati language pack
        2. Tesseract             - the program + guj.traineddata
        3. EasyOCR               - pip only, but has no Gujarati model yet

    Returns None when nothing can read Gujarati; detection then falls back to
    template matching, which is markedly weaker."""
    with _ocr_engine_lock:
        if _ocr_engine_cache:
            return _ocr_engine_cache[0]

        def log(text: str, level: str = "info") -> None:
            try:
                reporter.log(text, level)
            except Exception:
                pass

        # The full validator runs once per process, not once per edition.
        if not _ocr_setup_logged.is_set():
            _ocr_setup_logged.set()
            status, fixes = validate_ocr_setup()
            log("[OCR] Backend check:", "info")
            for line in status:
                log(f"        {line}", "dim")
            for line in fixes:
                log(f"[Setup] {line}", "warn")

        engine = _build_ocr_engine(log)
        if engine is None:
            log("[OCR] No Gujarati-capable backend -> detection is "
                "TEMPLATE-ONLY (weaker).  See the [Setup] lines above.",
                "error")
        _ocr_engine_cache.append(engine)
        return engine


# =============================================================================
# 7. DETECTION PIPELINE
# =============================================================================

@dataclass
class Detection:
    """A detected notice on the *working-scale* page."""
    rect: Tuple[int, int, int, int]
    score: float
    method: str


#: RAQM is a process-wide property of Pillow; warn about it once, not once
#: per edition agent (12 agents used to emit 12 identical warnings).
_raqm_warned = threading.Event()


def _font_renders_gujarati(font_path: str) -> bool:
    """True when the font actually draws real Gujarati glyphs.

    Two checks: the text must produce ink, and two DIFFERENT Gujarati letters
    must produce DIFFERENT masks - fonts without the script draw identical
    '.notdef' (tofu) boxes for every unsupported character."""
    try:
        font = ImageFont.truetype(font_path, 40)
        mask_a = font.getmask("જ")
        mask_b = font.getmask("મ")
        if mask_a.getbbox() is None or mask_b.getbbox() is None:
            return False
        if bytes(mask_a) == bytes(mask_b):
            return False          # identical => both are notdef boxes
        full = font.getmask("જાહેર").getbbox()
        return full is not None and (full[2] - full[0]) > 20
    except Exception:
        return False


@functools.lru_cache(maxsize=4)
def discover_gujarati_fonts(max_fonts: int = 2) -> List[str]:
    """Find fonts able to render Gujarati.  Known good names are tried first;
    if none of them exist (or fail the glyph test) the system font folders
    are scanned and every font is glyph-tested until enough are found.

    Cached: the walk glyph-tests up to 900 font files and the installed fonts
    do not change mid-run, but every edition agent builds a verifier."""
    found: List[str] = []

    for candidate in GUJARATI_FONT_CANDIDATES:
        if len(found) >= max_fonts:
            return found
        if os.path.isfile(candidate) and _font_renders_gujarati(candidate):
            found.append(candidate)
    if found:
        return found

    font_dirs = [
        r"C:\Windows\Fonts",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts"),
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        os.path.expanduser("~/.fonts"),
        os.path.expanduser("~/.local/share/fonts"),
        "/System/Library/Fonts/Supplemental",
        "/System/Library/Fonts",
        "/Library/Fonts",
    ]
    tested = 0
    for directory in font_dirs:
        if not os.path.isdir(directory):
            continue
        for root, _dirs, files in os.walk(directory):
            for name in sorted(files):
                if not name.lower().endswith((".ttf", ".ttc", ".otf")):
                    continue
                tested += 1
                if tested > 900:          # bound startup cost
                    return found
                path = os.path.join(root, name)
                if _font_renders_gujarati(path):
                    found.append(path)
                    if len(found) >= max_fonts:
                        return found
    return found


class HeaderTemplateVerifier:
    """Renders "જાહેર નોટિસ" (and variants) with system fonts and verifies
    candidate header strips via multi-scale normalized template matching.

    TM_CCOEFF_NORMED is mean-subtracted, so matching both the strip and its
    inverse also catches white-on-black header bands.
    """

    def __init__(self, config: DetectionConfig, reporter: ProgressReporter,
                 embedded_prefixes: Optional[Tuple[str, ...]] = None):
        self._cfg = config
        self._reporter = reporter
        # Which embedded templates to load, by label prefix (None = all).
        # Lets each newspaper pipeline use its own real-paper samples.
        self._embedded_prefixes = embedded_prefixes
        self.templates: List[Tuple[str, "np.ndarray"]] = []
        self.font_positive_templates: List[Tuple[str, "np.ndarray"]] = []
        self.negative_templates: List[Tuple[str, "np.ndarray"]] = []
        self.strong_negative_templates: \
            List[Tuple[str, "np.ndarray"]] = []
        self.have_gujarati_font = False
        self.have_embedded = False
        self._build_templates()

    # -- template construction ------------------------------------------------
    @staticmethod
    def _find_fonts(candidates: Tuple[str, ...],
                    globs: Tuple[str, ...] = ()) -> List[str]:
        found = [p for p in candidates if os.path.isfile(p)]
        for pattern in globs:
            for p in sorted(glob.glob(pattern, recursive=True)):
                if p not in found:
                    found.append(p)
        return found

    def _render_text(self, text: str, font_path: str) -> Optional["np.ndarray"]:
        """Render black text on white; return a tightly-cropped gray array."""
        try:
            size = self._cfg.template_render_px
            font = ImageFont.truetype(font_path, size)
            # Generous canvas; then crop to ink.
            canvas = Image.new("L", (size * len(text) + 80, size * 3), 255)
            draw = ImageDraw.Draw(canvas)
            draw.text((20, size // 2), text, font=font, fill=0)
            arr = np.array(canvas)
            ink = np.where(arr < 200)
            if ink[0].size == 0:
                return None
            y0, y1 = ink[0].min(), ink[0].max()
            x0, x1 = ink[1].min(), ink[1].max()
            crop = arr[max(0, y0 - 2): y1 + 3, max(0, x0 - 2): x1 + 3]
            if crop.shape[0] < 8 or crop.shape[1] < 24:
                return None
            return crop
        except Exception:
            return None

    def _build_templates(self) -> None:
        # 1) Embedded real-newspaper templates: always available, first
        #    priority (they use Gujarat Samachar's actual header typeface).
        for label, encoded in EMBEDDED_HEADER_TEMPLATES_B64:
            # "NEG:" entries are STRONG negative headers (e.g. the DB
            # "જાહેર નોટિસમાં સુધારો" pill) - loaded for the veto scan
            # only, never as positive matchers.
            is_negative = label.startswith("NEG:")
            base_label = label[4:] if is_negative else label
            # The embedded positives are all જાહેર નોટિસ crops - in
            # chetavni-only mode they must not accept boxes.
            if not is_negative and active_notice_type() == "chetavni":
                continue
            # Negative (veto) templates load for EVERY pipeline - a tender /
            # auction / સુધારો header is unwanted in every newspaper.  Only
            # POSITIVE templates are filtered by this paper's prefixes.
            if (not is_negative) and self._embedded_prefixes is not None \
                    and not any(base_label.startswith(p)
                                for p in self._embedded_prefixes):
                continue
            try:
                raw = base64.b64decode(encoded)
                array = np.frombuffer(raw, dtype=np.uint8)
                template = cv2.imdecode(array, cv2.IMREAD_GRAYSCALE)
                if template is not None and template.shape[0] >= 12:
                    if is_negative:
                        self.strong_negative_templates.append(
                            (base_label, template))
                    else:
                        self.templates.append((label, template))
            except Exception:
                continue
        self.have_embedded = bool(self.templates)

        # 2) Font-rendered templates as extra variants.
        guj_fonts = discover_gujarati_fonts(max_fonts=2)
        eng_fonts = self._find_fonts(ENGLISH_FONT_CANDIDATES)[:1]
        self.have_gujarati_font = bool(guj_fonts)

        have_raqm = False
        try:
            have_raqm = bool(pil_features.check("raqm"))
        except Exception:
            pass

        mode = active_notice_type()
        variants = [(text, script) for text, script in HEADER_VARIANTS
                    if mode == "all"
                    or (mode == "chetavni") == ("ચેતવણ" in text)]
        if not have_raqm:
            # Complex-script shaping unavailable: add visual-order fallbacks.
            # This only degrades the EXTRA font-rendered templates - the
            # embedded base64 templates were shaped correctly at build time,
            # and OCR (when active) is what actually decides a match.
            if mode != "chetavni":      # these fallbacks are all નોટિસ forms
                variants.extend(HEADER_VARIANTS_NO_RAQM)
            if not _raqm_warned.is_set():        # once per process, not per agent
                _raqm_warned.set()
                self._reporter.log(
                    "[Warning] RAQM not available -> fallback rendering "
                    "(embedded templates are unaffected; OCR decides "
                    "matches).", "warn")
                self._reporter.log(
                    "          Pillow's Windows wheels do not bundle "
                    "libraqm and there is no 'pillow[raqm]' extra, so "
                    "pip cannot fix this - see setup_ocr.py --raqm.", "dim")

        for text, script in variants:
            fonts = guj_fonts if script == "guj" else eng_fonts
            for font_path in fonts:
                rendered = self._render_text(text, font_path)
                if rendered is not None:
                    label = f"{text} [{os.path.basename(font_path)}]"
                    self.templates.append((label, rendered))
                    self.font_positive_templates.append((label, rendered))

        # 3) NEGATIVE templates (tender / auction / possession headers).
        #    Rendered with the same fonts; used only to veto, never accept.
        for text, script in NEGATIVE_HEADER_VARIANTS:
            fonts = guj_fonts if script == "guj" else eng_fonts
            for font_path in fonts:
                rendered = self._render_text(text, font_path)
                if rendered is not None:
                    label = f"NEG {text} [{os.path.basename(font_path)}]"
                    self.negative_templates.append((label, rendered))

        if not self.templates:
            self._reporter.log(
                "No header templates could be loaded.", "warn")

    @property
    def has_templates(self) -> bool:
        return bool(self.templates)

    @property
    def gujarati_capable(self) -> bool:
        return self.have_embedded or self.have_gujarati_font

    # -- matching -------------------------------------------------------------
    def _match_one(self, strip: "np.ndarray", template: "np.ndarray",
                   target_heights: Tuple[int, ...]) -> float:
        best = 0.0
        sh, sw = strip.shape[:2]
        th0, tw0 = template.shape[:2]
        for target_h in target_heights:
            if target_h > sh - 2:
                continue
            scale = target_h / float(th0)
            tw = max(1, int(round(tw0 * scale)))
            if tw >= sw - 2 or tw < 24:
                continue
            resized = cv2.resize(template, (tw, target_h),
                                 interpolation=cv2.INTER_AREA)
            # One pass: |score| also covers the inverted (white-on-black)
            # case, because inverting the image negates TM_CCOEFF_NORMED.
            result = cv2.matchTemplate(strip, resized, cv2.TM_CCOEFF_NORMED)
            value = float(np.abs(result).max())
            if value > best:
                best = value
        return best

    def strip_score(self, strip: "np.ndarray") -> float:
        """Best template score for one candidate header strip."""
        if strip.size == 0 or not self.templates:
            return 0.0
        best = 0.0
        for _, template in self.templates:
            best = max(best, self._match_one(strip, template,
                                             self._cfg.strip_scales))
        return best

    def negative_strip_score(self, strip: "np.ndarray") -> float:
        """Best NEGATIVE (tender / auction) template score for one strip."""
        if strip.size == 0 or not self.negative_templates:
            return 0.0
        best = 0.0
        for _, template in self.negative_templates:
            best = max(best, self._match_one(strip, template,
                                             self._cfg.strip_scales))
        return best

    def font_positive_strip_score(self, strip: "np.ndarray") -> float:
        """Best score among FONT-RENDERED positive templates only - the
        fair baseline to compare font-rendered negatives against (embedded
        real-paper crops score on a different scale than font renders)."""
        if strip.size == 0 or not self.font_positive_templates:
            return 0.0
        best = 0.0
        for _, template in self.font_positive_templates:
            best = max(best, self._match_one(strip, template,
                                             self._cfg.strip_scales))
        return best

    def strong_negative_page_scan(self, gray: "np.ndarray"
                                  ) -> List[Tuple[int, int, int, int,
                                                  float]]:
        """Locate STRONG negative headers (e.g. સુધારો pills) anywhere on
        the page.  Returns hit rects (x, y, w, h, score)."""
        hits: List[Tuple[int, int, int, int, float]] = []
        for _, template in self.strong_negative_templates:
            th0, tw0 = template.shape[:2]
            for target_h in STRONG_NEGATIVE_SCALES:
                scale = target_h / float(th0)
                tw = max(1, int(round(tw0 * scale)))
                if tw < 30 or tw >= gray.shape[1] - 2 or \
                        target_h >= gray.shape[0] - 2:
                    continue
                resized = cv2.resize(template, (tw, target_h),
                                     interpolation=cv2.INTER_AREA)
                result = np.abs(cv2.matchTemplate(gray, resized,
                                                  cv2.TM_CCOEFF_NORMED))
                ys, xs = np.where(result >= STRONG_NEGATIVE_PAGE_THRESHOLD)
                for x, y in zip(xs.tolist(), ys.tolist()):
                    hits.append((x, y, tw, target_h, float(result[y, x])))
        return self._nms_hits(hits)

    def page_scan(self, gray: "np.ndarray", threshold: float,
                  scales: Optional[Tuple[int, ...]] = None
                  ) -> List[Tuple[int, int, int, int, float]]:
        """Sweep an image for header occurrences.  Returns hit rects
        (x, y, w, h, score).  Only the first (highest-priority) few templates
        are used to keep this fast."""
        hits: List[Tuple[int, int, int, int, float]] = []
        if not self.templates:
            return hits
        for _, template in self.templates[:6]:
            if self._reporter is not None:
                self._reporter.check_cancel()
            th0, tw0 = template.shape[:2]
            for target_h in (scales or self._cfg.page_scan_scales):
                scale = target_h / float(th0)
                tw = max(1, int(round(tw0 * scale)))
                if tw < 30 or tw >= gray.shape[1] - 2 or \
                        target_h >= gray.shape[0] - 2:
                    continue
                resized = cv2.resize(template, (tw, target_h),
                                     interpolation=cv2.INTER_AREA)
                result = np.abs(cv2.matchTemplate(gray, resized,
                                                  cv2.TM_CCOEFF_NORMED))
                ys, xs = np.where(result >= threshold)
                for x, y in zip(xs.tolist(), ys.tolist()):
                    hits.append((x, y, tw, target_h, float(result[y, x])))
        return self._nms_hits(hits)

    @staticmethod
    def _nms_hits(hits: List[Tuple[int, int, int, int, float]]
                  ) -> List[Tuple[int, int, int, int, float]]:
        hits = sorted(hits, key=lambda h: -h[4])
        kept: List[Tuple[int, int, int, int, float]] = []
        for hit in hits:
            rect = hit[:4]
            if all(rect_iou(rect, k[:4]) < 0.30 and
                   rect_containment(rect, k[:4]) < 0.6 for k in kept):
                kept.append(hit)
        return kept

    @staticmethod
    def prepare_strip_for_ocr(strip: "np.ndarray") -> "np.ndarray":
        """Make a header strip OCR-friendly: dark-on-light polarity, decent
        size, and a white margin (OCR engines dislike edge-touching text)."""
        prepared = strip
        if float(np.mean(prepared)) < 110:      # white-on-black header band
            prepared = 255 - prepared
        if prepared.shape[0] < OCR_STRIP_TARGET_HEIGHT:
            factor = OCR_STRIP_TARGET_HEIGHT / float(prepared.shape[0])
            prepared = cv2.resize(prepared, None, fx=factor, fy=factor,
                                  interpolation=cv2.INTER_CUBIC)
        return cv2.copyMakeBorder(prepared, 12, 12, 12, 12,
                                  cv2.BORDER_CONSTANT, value=255)


class BoxCandidateDetector:
    """Finds rectangular, ruled boxes on a newspaper page using morphological
    line extraction - the standard, OCR-free way to find bordered regions."""

    def __init__(self, config: DetectionConfig):
        self._cfg = config
        self.horizontal_mask: Optional["np.ndarray"] = None
        self.vertical_mask: Optional["np.ndarray"] = None

    def compute_line_masks(self, gray: "np.ndarray") -> None:
        height, width = gray.shape[:2]
        binary = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
            cv2.THRESH_BINARY_INV, 15, 9)
        h_len = max(24, int(width * 0.035))
        v_len = max(24, int(height * 0.018))
        h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (h_len, 1))
        v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_len))
        self.horizontal_mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
                                                h_kernel)
        self.vertical_mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
                                              v_kernel)

    def _border_coverage(self, rect: Tuple[int, int, int, int]
                         ) -> Tuple[float, float, float, float, float]:
        """Coverage of each rect side by line pixels: (total, top, bottom,
        left, right)."""
        x, y, w, h = rect
        hmask, vmask = self.horizontal_mask, self.vertical_mask
        band = 4  # tolerance band around each edge

        def _h_cov(edge_y: int) -> float:
            y0 = clamp(edge_y - band, 0, hmask.shape[0] - 1)
            y1 = clamp(edge_y + band + 1, 1, hmask.shape[0])
            seg = hmask[y0:y1, x:x + w]
            if seg.size == 0:
                return 0.0
            return float(np.count_nonzero(seg.max(axis=0))) / max(1, w)

        def _v_cov(edge_x: int) -> float:
            x0 = clamp(edge_x - band, 0, vmask.shape[1] - 1)
            x1 = clamp(edge_x + band + 1, 1, vmask.shape[1])
            seg = vmask[y:y + h, x0:x1]
            if seg.size == 0:
                return 0.0
            return float(np.count_nonzero(seg.max(axis=1))) / max(1, h)

        top = _h_cov(y)
        bottom = _h_cov(y + h)
        left = _v_cov(x)
        right = _v_cov(x + w)
        total = (top * w + bottom * w + left * h + right * h) / (2.0 * (w + h))
        return total, top, bottom, left, right

    def find_candidates(self, gray: "np.ndarray"
                        ) -> Tuple[List[Tuple[int, int, int, int]],
                                   List[Tuple[int, int, int, int]]]:
        """Returns (filtered_candidates, all_rects).

        filtered_candidates - notice-sized, well-bordered boxes.
        all_rects           - every closed rect found (used to attach
                              full-page header hits to their enclosing box).
        """
        cfg = self._cfg
        height, width = gray.shape[:2]
        self.compute_line_masks(gray)

        grid = cv2.add(self.horizontal_mask, self.vertical_mask)
        grid = cv2.dilate(grid, cv2.getStructuringElement(
            cv2.MORPH_RECT, (3, 3)), iterations=1)

        contours, _ = cv2.findContours(grid, cv2.RETR_LIST,
                                       cv2.CHAIN_APPROX_SIMPLE)
        min_w = int(width * cfg.box_min_w_frac)
        max_w = int(width * cfg.box_max_w_frac)
        max_h = int(height * cfg.box_max_h_frac)

        all_rects: List[Tuple[int, int, int, int]] = []
        filtered: List[Tuple[int, int, int, int]] = []
        seen: set = set()

        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            key = (x // 4, y // 4, w // 4, h // 4)
            if key in seen:
                continue
            seen.add(key)
            if w < 40 or h < 30 or w > width - 4:
                continue
            all_rects.append((x, y, w, h))

            if not (min_w <= w <= max_w):
                continue
            if not (cfg.box_min_h_px <= h <= max_h):
                continue
            aspect = h / float(w)
            if not (cfg.box_min_aspect <= aspect <= cfg.box_max_aspect):
                continue
            total, top, bottom, left, right = self._border_coverage((x, y, w, h))
            if total < cfg.border_coverage_total:
                continue
            sides_ok = sum(c >= cfg.border_coverage_side
                           for c in (top, bottom, left, right))
            if sides_ok < 3:
                continue
            filtered.append((x, y, w, h))

        return filtered, all_rects

    # -- box synthesis for header hits without a clean candidate box ----------
    def synthesize_box(self, hit: Tuple[int, int, int, int],
                       gray_shape: Tuple[int, int]
                       ) -> Optional[Tuple[int, int, int, int]]:
        """Given a header hit (x, y, w, h), derive a plausible notice box from
        the nearest ruling lines around it.  Returns None when the local
        structure is too ambiguous (we never invent detections)."""
        height, width = gray_shape[:2]
        hx, hy, hw, hh = hit
        cx = hx + hw // 2
        vmask, hmask = self.vertical_mask, self.horizontal_mask
        column_w = width // 8  # typical broadsheet column width

        # Vertical boundaries: nearest strong vertical lines left/right of the
        # hit, evaluated over a window below the header.
        y0 = clamp(hy - 10, 0, height - 1)
        y1 = clamp(hy + int(column_w * 2.5), y0 + 20, height)
        window = vmask[y0:y1, :]
        col_cov = (window > 0).sum(axis=0) / float(max(1, y1 - y0))
        strong_cols = np.where(col_cov > 0.45)[0]

        left_side = strong_cols[strong_cols < hx - 4]
        right_side = strong_cols[strong_cols > hx + hw + 4]
        x_left = int(left_side.max()) if left_side.size else \
            clamp(cx - column_w, 0, width - 1)
        x_right = int(right_side.min()) if right_side.size else \
            clamp(cx + column_w, x_left + 60, width - 1)
        if x_right - x_left < 60 or x_right - x_left > width * 0.7:
            return None

        # Top boundary: nearest horizontal line just above the header.
        row_span = hmask[:, x_left:x_right]
        row_cov = (row_span > 0).sum(axis=1) / float(max(1, x_right - x_left))
        above = np.where(row_cov[:hy] > 0.45)[0] if hy > 0 else np.array([])
        top = int(above.max()) if above.size and hy - above.max() < 140 \
            else clamp(hy - 8, 0, height - 1)

        # Bottom boundary.  Notices often contain internal dividers directly
        # under the title, so the FIRST line below is not necessarily the
        # bottom border.  Among candidate lines, prefer the lowest one whose
        # extent is still supported by the left/right side rules.
        search_from = clamp(hy + hh + 40, 0, height - 1)
        max_bottom = clamp(top + int(height * 0.65), search_from + 1, height)
        below = np.where(row_cov[search_from:max_bottom] > 0.45)[0]

        def _side_support(y_bottom: int) -> float:
            """How far down (fraction) the side rules extend towards
            y_bottom."""
            span = max(1, y_bottom - top)
            support = []
            for edge_x in (x_left, x_right):
                x0 = clamp(edge_x - 4, 0, width - 1)
                x1 = clamp(edge_x + 5, x0 + 1, width)
                column = vmask[top:y_bottom, x0:x1]
                support.append(np.count_nonzero(column.max(axis=1)) /
                               float(span))
            return min(support)

        bottom = None
        if below.size:
            # Cluster consecutive rows into distinct lines, lowest first.
            line_rows = below + search_from
            clusters = [int(line_rows[0])]
            for row in line_rows[1:]:
                if row - clusters[-1] > 6:
                    clusters.append(int(row))
                else:
                    clusters[-1] = int(row)
            for candidate in reversed(clusters):
                if _side_support(candidate) >= 0.55:
                    bottom = candidate
                    break
            if bottom is None:
                bottom = clusters[0]  # best available guess
        if bottom is None:
            bottom = clamp(hy + int(column_w * 2.2), search_from, height - 1)
        if bottom - top < 70:
            return None
        return (x_left, top, x_right - x_left, bottom - top)


class NoticeDetectionPipeline:
    """The complete per-page detection pipeline (shared by all newspapers -
    the templates and keywords target Gujarati જાહેર નોટિસ headers).

    Hybrid strategy:
      Pass 1 - CV finds bordered box candidates; each candidate's header
               strip is verified by template matching (cheap) and, when that
               is inconclusive, by Gujarati OCR of just that strip.
      Pass 2 - full-page template sweep recovers notices with broken borders.
      Pass 3 - on pages where NOTHING was found, a full-page OCR sweep looks
               for "જાહેર" + "નોટિસ" word pairs as a safety net.
    """

    # Subclasses (one per newspaper) override these:
    newspaper_name: str = "generic"
    default_config: DetectionConfig = DETECTION_CONFIG
    embedded_prefixes: Optional[Tuple[str, ...]] = None

    def __init__(self, config: Optional[DetectionConfig] = None,
                 reporter: ProgressReporter = None,
                 ocr_engine: Optional[BaseOcrEngine] = None,
                 broad: bool = False):
        self._cfg = config or self.default_config
        self._reporter = reporter
        self._ocr = ocr_engine
        self._broad = broad
        self.verifier = HeaderTemplateVerifier(
            self._cfg, reporter, embedded_prefixes=self.embedded_prefixes)
        self.boxes = BoxCandidateDetector(self._cfg)
        #: template score of every bordered candidate on the last page -
        #: exported for the zero-result diagnostic report.
        self.last_candidate_scores: List[float] = []

    @property
    def usable(self) -> bool:
        return self.verifier.has_templates or self._ocr is not None

    # -- parallel OCR ---------------------------------------------------------
    def _ocr_read_one(self, prepared: "np.ndarray") -> str:
        try:
            # Stop a multi-pass engine as soon as a pass already reads as a
            # notice header - the extra passes only exist to rescue misses.
            return self._ocr.read_text(
                prepared,
                early_stop=lambda text:
                    match_notice_text(text, self._broad)[0] > 0)
        except Exception:
            return ""

    def _ocr_batch(self, strips: List["np.ndarray"]) -> List[str]:
        """Read several header strips at once.  Returns one text per strip,
        in the same order (empty string when a strip could not be read).

        The detect gate is handed back while this waits.  OCR is a Tesseract
        subprocess (or a WinRT call) and is already bounded by its own pool -
        holding the detect slot through that wait would let one agent's OCR
        block another agent's template matching for no reason."""
        if self._ocr is None or not strips:
            return [""] * len(strips)
        prepared = [self.verifier.prepare_strip_for_ocr(s) for s in strips]
        self._reporter.check_cancel()
        with detect_gate_released():
            if len(prepared) == 1:
                return [self._ocr_read_one(prepared[0])]
            # One process-wide pool instead of a fresh one per page.
            pool = get_ocr_pool()
            return list(pool.map(self._ocr_read_one, prepared))

    def detect(self, page_bgr: "np.ndarray") -> List[Detection]:
        """Detect all public notices on one page.  Returns detections in
        working-scale coordinates plus the scale factor via .rect mapping done
        by the caller (see detect_and_crop).

        Template matching is the CPU floor of the whole app, so the actual
        work is taken under a process-wide gate: any number of edition agents
        may call this, only DETECT_CONCURRENCY of them compute at once."""
        with detect_gate_held():
            return self._detect_page(page_bgr)

    def _detect_page(self, page_bgr: "np.ndarray") -> List[Detection]:
        cfg = self._cfg
        gray_full = cv2.cvtColor(page_bgr, cv2.COLOR_BGR2GRAY)

        # Work at a bounded resolution; crops are made from the original.
        height, width = gray_full.shape[:2]
        if width > cfg.working_width:
            self._scale = cfg.working_width / float(width)
            gray = cv2.resize(gray_full, (cfg.working_width,
                                          int(height * self._scale)),
                              interpolation=cv2.INTER_AREA)
        else:
            self._scale = 1.0
            gray = gray_full

        candidates, all_rects = self.boxes.find_candidates(gray)
        detections: List[Detection] = []
        self.last_candidate_scores = []

        # --- Pass 1: bordered boxes, verified by template AND/OR OCR --------
        # Template scoring first (cheap, CPU), then every borderline strip
        # is OCR'd by the worker pool in one go instead of one by one.
        undecided: List[Tuple[Tuple[int, int, int, int], "np.ndarray",
                              float]] = []
        for rect in candidates:
            self._reporter.check_cancel()
            strip = self._header_strip(gray, rect)
            score = self.verifier.strip_score(strip)
            self.last_candidate_scores.append(round(float(score), 3))
            if score >= cfg.box_match_threshold:
                detections.append(Detection(rect, score, "box+template"))
                continue
            if self._ocr is not None and \
                    len(undecided) < OCR_MAX_STRIPS_PER_PAGE and \
                    self._strip_has_ink(strip):
                undecided.append((rect, strip, score))

        if undecided:
            texts = self._ocr_batch([strip for _r, strip, _s in undecided])
            for (rect, _strip, score), text in zip(undecided, texts):
                ratio, _kw = match_notice_text(text, self._broad)
                if ratio > 0:
                    combined = max(score, 0.50 + 0.45 * ratio)
                    detections.append(Detection(rect, min(combined, 0.97),
                                                "box+ocr"))

        # --- Pass 2: full-page template sweep (broken borders) --------------
        hits = self.verifier.page_scan(gray, cfg.page_match_threshold)
        self._attach_hits(hits, detections, all_rects, gray, "page-scan")

        # --- Pass 3: full-page OCR sweep on empty pages (safety net) --------
        if not detections and self._ocr is not None:
            self._reporter.log("  Deep scan: full-page OCR sweep...", "dim")
            ocr_hits = self._ocr_page_sweep(gray)
            self._attach_hits(ocr_hits, detections, all_rects, gray,
                              "ocr-sweep")

        # Drop borderline low-confidence matches (stray photos, tables) -
        # box+ocr detections carry their own OCR confidence and are exempt.
        detections = [d for d in detections
                      if d.score >= GLOBAL_MIN_ACCEPT_SCORE
                      or "ocr" in d.method]
        detections = self._deduplicate(detections)
        # --- Pass 4: split crops that contain SEVERAL notices ---------------
        detections = self._split_merged(detections, gray)
        # --- Pass 5: a crop fully containing another crop is a merged
        #     duplicate (notices never nest) - keep the smaller ones. -------
        detections = self._drop_containers(detections)
        # --- Pass 6: veto tender / auction / recruitment look-alikes --------
        detections = self._reject_negatives(gray, detections)
        return detections

    @staticmethod
    def _drop_containers(detections: List[Detection]) -> List[Detection]:
        kept: List[Detection] = []
        for det in sorted(detections,
                          key=lambda d: d.rect[2] * d.rect[3]):  # small first
            area = det.rect[2] * det.rect[3]
            swallows_kept = any(
                rect_containment(k.rect, det.rect) >= 0.62
                and area > 1.35 * k.rect[2] * k.rect[3]
                for k in kept)
            if not swallows_kept:
                kept.append(det)
        kept.sort(key=lambda d: (d.rect[1], d.rect[0]))
        return kept

    # -- negative vetting (v1.8) ----------------------------------------------
    # Tender notices (જાહેર નિવિદા / ઈ-ટેન્ડર નોટિસ), e-auction, possession
    # and sale notices and recruitment ads share words and typography with
    # જાહેર નોટિસ and can sneak past template matching.  Every detection's
    # header strip therefore gets a final check: when it matches the NEGATIVE
    # vocabulary / templates better than a real notice header, it is dropped.
    def _reject_negatives(self, gray: "np.ndarray",
                          detections: List[Detection]) -> List[Detection]:
        strong_hits: List[Tuple[int, int, int, int, float]] = []
        if detections and self.verifier.strong_negative_templates:
            strong_hits = self.verifier.strong_negative_page_scan(gray)
        # OCR for the veto is also done in one parallel batch.
        texts: Dict[int, str] = {}
        if self._ocr is not None and detections:
            batch: List[Tuple[int, "np.ndarray"]] = []
            for index, det in enumerate(detections):
                if self._header_has_hit(det.rect, strong_hits):
                    continue
                strip = self._header_strip(gray, det.rect)
                if strip.size and self._strip_has_ink(strip):
                    batch.append((index, strip))
            if batch:
                read = self._ocr_batch([s for _i, s in batch])
                texts = {index: text
                         for (index, _s), text in zip(batch, read)}

        kept: List[Detection] = []
        for index, det in enumerate(detections):
            self._reporter.check_cancel()
            if self._header_has_hit(det.rect, strong_hits):
                self._reporter.log(
                    "  Skipped one box: 'નોટિસમાં સુધારો' variant header "
                    "(not a plain Public Notice)", "dim")
                continue
            reason = self._negative_reason(gray, det, texts.get(index))
            if reason:
                self._reporter.log(
                    f"  Skipped one box: '{reason}' header "
                    "(tender/auction/ad - not a Public Notice)", "dim")
            else:
                kept.append(det)
        return kept

    @staticmethod
    def _header_has_hit(rect: Tuple[int, int, int, int],
                        hits: List[Tuple[int, int, int, int, float]]
                        ) -> bool:
        """True when a strong-negative pill center sits in the top part of
        this detection (the pill may be wider than the detected box)."""
        x, y, w, h = rect
        top_h = max(40, int(h * 0.45))
        for hx, hy, hw, hh, _score in hits:
            cx = hx + hw // 2
            cy = hy + hh // 2
            if x - 10 <= cx <= x + w + 10 and y - 10 <= cy <= y + top_h:
                return True
        return False

    def _negative_reason(self, gray: "np.ndarray", det: Detection,
                         text: Optional[str] = None) -> Optional[str]:
        """Why this detection should be rejected - or None to keep it.

        `text` is the OCR of this detection's header strip when it was read
        by the parallel batch; None means "read it here if needed"."""
        strip = self._header_strip(gray, det.rect)
        if strip.size == 0:
            return None
        # (a) OCR verdict - the most reliable signal when available.
        if self._ocr is not None and self._strip_has_ink(strip):
            if text is None:
                prepared = self.verifier.prepare_strip_for_ocr(strip)
                try:
                    text = self._ocr.read_text(prepared)
                except Exception:
                    text = ""
            if text.strip():
                normalized_text = normalize_ocr_text(text)
                for keyword in NEGATIVE_OVERRIDE_KEYWORDS:
                    if fuzzy_contains(normalized_text,
                                      normalize_ocr_text(keyword),
                                      NEGATIVE_FUZZY_RATIO):
                        return keyword
                neg_ratio, neg_kw = match_negative_text(text)
                pos_ratio, _kw = match_notice_text(text, self._broad)
                if neg_ratio > 0 and pos_ratio < neg_ratio:
                    return neg_kw
                if pos_ratio > 0:
                    return None      # clearly a real notice header
        # (b) template verdict - works without OCR.  Font-rendered negatives
        # are compared against font-rendered POSITIVES (like against like,
        # since font renders score lower than the embedded real-paper crops);
        # a strong overall positive is always trusted and never vetoed.
        neg = self.verifier.negative_strip_score(strip)
        if neg >= NEGATIVE_TEMPLATE_MIN and \
                self.verifier.strip_score(strip) < NEGATIVE_TRUST_POS:
            font_pos = self.verifier.font_positive_strip_score(strip)
            if neg >= font_pos + NEGATIVE_TEMPLATE_MARGIN:
                return "tender/auction style"
        return None

    # -- splitting merged notices ---------------------------------------------
    # Two notices side-by-side or stacked can end up inside one detected box
    # (shared outer border / attached hit box).  If a crop contains multiple
    # જાહેર નોટિસ headers it is split along the ruling lines between them.
    def _split_merged(self, detections: List[Detection],
                      gray: "np.ndarray") -> List[Detection]:
        result: List[Detection] = []
        for det in detections:
            result.extend(self._split_one(det, gray))
        result.sort(key=lambda d: (d.rect[1], d.rect[0]))
        return result

    def _split_one(self, det: Detection,
                   gray: "np.ndarray") -> List[Detection]:
        x, y, w, h = det.rect
        if w < 140 and h < 140:
            return [det]
        pad = 4
        rx0 = int(clamp(x - pad, 0, gray.shape[1] - 1))
        ry0 = int(clamp(y - pad, 0, gray.shape[0] - 1))
        region = gray[ry0:y + h + pad, rx0:x + w + pad]
        if region.shape[0] < 90 or region.shape[1] < 90:
            return [det]
        # Rescan the crop with the full (finer) scale range - second headers
        # are often smaller than the page-sweep scales - but at the SAME
        # confidence threshold, otherwise body text on low-quality pages
        # produces phantom "headers" and fragments the crop.
        hits = self.verifier.page_scan(
            region, self._cfg.box_match_threshold,
            scales=self._cfg.strip_scales)
        if len(hits) <= 1 or len(hits) > 6:
            return [det]          # nothing to split / too noisy to trust
        hits = [(hx + rx0, hy + ry0, hw, hh, s)
                for hx, hy, hw, hh, s in hits]

        # Cluster headers into visual rows (stacked notices differ in y).
        hits.sort(key=lambda t: t[1])
        rows: List[List[tuple]] = [[hits[0]]]
        for hit in hits[1:]:
            if hit[1] - rows[-1][0][1] < max(hit[3], rows[-1][0][3]) * 2.5:
                rows[-1].append(hit)
            else:
                rows.append([hit])

        # Horizontal boundaries between stacked rows.
        y_bounds = [y]
        for row in rows[1:]:
            row_top = min(hh[1] for hh in row)
            line = self._nearest_hline(row_top - 4, x, x + w)
            y_bounds.append(line if line is not None else row_top - 4)
        y_bounds.append(y + h)

        cells: List[Detection] = []
        for row_index, row in enumerate(rows):
            cy0, cy1 = y_bounds[row_index], y_bounds[row_index + 1]
            if cy1 - cy0 < 70:
                continue
            row.sort(key=lambda t: t[0])
            x_bounds = [x]
            for i in range(1, len(row)):
                left_end = row[i - 1][0] + row[i - 1][2]
                right_start = row[i][0]
                if right_start - left_end < 55:
                    continue      # same title matched twice - ignore
                middle = (left_end + right_start) // 2
                line = self._nearest_vline(middle, cy0, cy1,
                                           left_end, right_start)
                x_bounds.append(line if line is not None else middle)
            x_bounds.append(x + w)
            for i in range(len(x_bounds) - 1):
                cw = x_bounds[i + 1] - x_bounds[i]
                if cw >= 70:
                    cells.append(Detection(
                        (x_bounds[i], cy0, cw, cy1 - cy0),
                        det.score, det.method + "+split"))
        if len(cells) < 2:
            return [det]
        # Sanity: every cell must have its own header in its TOP part -
        # otherwise the extra "headers" were phantom matches in body text.
        for cell in cells:
            cx0, cy0, cw, ch = cell.rect
            has_top_header = any(
                cx0 <= hx + hw2 // 2 <= cx0 + cw
                and cy0 <= hy + hh2 // 2 <= cy0 + int(ch * 0.45)
                for hx, hy, hw2, hh2, _s in hits)
            if not has_top_header:
                return [det]
        return cells

    def _nearest_hline(self, y_target: int, x0: int,
                       x1: int) -> Optional[int]:
        """Strongest horizontal ruling line within +-28 px of y_target."""
        hmask = self.boxes.horizontal_mask
        if hmask is None:
            return None
        lo = int(clamp(y_target - 28, 0, hmask.shape[0] - 1))
        hi = int(clamp(y_target + 28, lo + 1, hmask.shape[0]))
        span = hmask[lo:hi, int(clamp(x0, 0, hmask.shape[1] - 1)):
                     int(clamp(x1, 1, hmask.shape[1]))]
        if span.size == 0:
            return None
        coverage = (span > 0).mean(axis=1)
        best = int(np.argmax(coverage))
        return lo + best if coverage[best] > 0.40 else None

    def _nearest_vline(self, x_target: int, y0: int, y1: int,
                       x_min: int, x_max: int) -> Optional[int]:
        """Strongest vertical ruling line between two side-by-side titles."""
        vmask = self.boxes.vertical_mask
        if vmask is None:
            return None
        lo = int(clamp(x_min, 0, vmask.shape[1] - 1))
        hi = int(clamp(x_max, lo + 1, vmask.shape[1]))
        span = vmask[int(clamp(y0, 0, vmask.shape[0] - 1)):
                     int(clamp(y1, 1, vmask.shape[0])), lo:hi]
        if span.size == 0:
            return None
        coverage = (span > 0).mean(axis=0)
        candidates = np.where(coverage > 0.45)[0]
        if candidates.size == 0:
            return None
        return lo + int(candidates[np.argmin(np.abs(
            candidates + lo - x_target))])

    # -- hit attachment (shared by template sweep and OCR sweep) --------------
    def _attach_hits(self, hits: List[Tuple[int, int, int, int, float]],
                     detections: List[Detection],
                     all_rects: List[Tuple[int, int, int, int]],
                     gray: "np.ndarray", method: str) -> None:
        cfg = self._cfg
        for hx, hy, hw, hh, hscore in hits:
            self._reporter.check_cancel()
            hit_rect = (hx, hy, hw, hh)
            hit_center = (hx + hw // 2, hy + hh // 2)
            if any(point_in_rect(*hit_center, d.rect) for d in detections):
                continue  # already covered by a verified box

            # Attach to the smallest closed rect that properly contains the
            # whole header hit (not merely its center) and is plausibly a
            # notice body - i.e. wider and clearly taller than the title.
            enclosing = [r for r in all_rects
                         if rect_containment(hit_rect, r) >= 0.80
                         and r[2] >= hw * 1.02
                         and r[3] >= max(cfg.box_min_h_px, hh * 1.6)
                         and r[2] <= gray.shape[1] * cfg.box_max_w_frac
                         and r[3] <= gray.shape[0] * cfg.box_max_h_frac]
            if enclosing:
                rect = min(enclosing, key=lambda r: r[2] * r[3])
                detections.append(Detection(rect, hscore, method + "+box"))
                continue

            synthesized = self.boxes.synthesize_box(hit_rect, gray.shape)
            if synthesized is not None:
                detections.append(Detection(synthesized, hscore * 0.9,
                                            method))

    # -- full-page OCR sweep ---------------------------------------------------
    def _ocr_page_sweep(self, gray: "np.ndarray"
                        ) -> List[Tuple[int, int, int, int, float]]:
        """Locate notice-title words anywhere on the page via OCR word boxes.
        Looks for "જાહેર" followed by "નોટિસ" on the same line (and the
        single-word / English forms)."""
        # This safety net runs on MOST pages (every page with no detections),
        # so its cost dominates a run.  Two cuts, benchmarked together:
        #   * OCR a downscaled copy - notice titles are display-size glyphs
        #     and survive it; Tesseract time scales with pixel count.
        #   * hand the detect slot back while the subprocess runs, so some
        #     other agent's template matching proceeds instead of idling.
        img = gray
        scale = 1.0
        if gray.shape[1] > SWEEP_MAX_WIDTH:
            scale = SWEEP_MAX_WIDTH / float(gray.shape[1])
            img = cv2.resize(gray, (SWEEP_MAX_WIDTH,
                                    max(1, int(gray.shape[0] * scale))),
                             interpolation=cv2.INTER_AREA)
        try:
            with detect_gate_released():
                words = self._ocr.read_words(img)
        except Exception as exc:
            self._reporter.log(f"  OCR sweep failed: {exc}", "warn")
            return []
        if not words:
            return []
        if scale != 1.0:
            words = [OcrWord(w.text, int(w.x / scale), int(w.y / scale),
                             max(1, int(w.w / scale)),
                             max(1, int(w.h / scale)), w.conf)
                     for w in words]

        normalized = [(normalize_ocr_text(w.text), w) for w in words]
        hits: List[Tuple[int, int, int, int, float]] = []

        # Single words already containing the full title (e.g. જાહેરનોટિસ).
        mode = active_notice_type()
        singles = [] if mode == "chetavni" else list(SWEEP_SINGLE_WORDS)
        if self._broad:
            singles += ["નોટિસ", "નોટીસ", "notice"]
        for norm, word in normalized:
            if len(norm) < 4:
                continue
            for target in singles:
                if fuzzy_contains(norm, normalize_ocr_text(target), 0.78):
                    hits.append((word.x, word.y, word.w, word.h,
                                 max(0.62, word.conf)))
                    break

        # Word pairs on the same text line ("જાહેર" ... "નોટિસ"), filtered
        # by the run's notice-type toggle.
        pairs = [(f, s2) for f, s2 in SWEEP_WORD_PAIRS
                 if mode == "all"
                 or (mode == "chetavni") == ("ચેત" in s2)]
        for first, second in pairs:
            first_n = normalize_ocr_text(first)
            second_n = normalize_ocr_text(second)
            starts = [w for n, w in normalized
                      if n and fuzzy_contains(n, first_n, 0.75)]
            ends = [w for n, w in normalized
                    if n and fuzzy_contains(n, second_n, 0.75)]
            for w1 in starts:
                for w2 in ends:
                    if w2 is w1:
                        continue
                    line_h = max(w1.h, w2.h)
                    same_line = abs((w2.y + w2.h / 2) -
                                    (w1.y + w1.h / 2)) < line_h * 0.7
                    gap = w2.x - (w1.x + w1.w)
                    if same_line and -line_h * 0.5 <= gap < line_h * 4:
                        x = min(w1.x, w2.x)
                        y = min(w1.y, w2.y)
                        hits.append((
                            x, y,
                            max(w1.x + w1.w, w2.x + w2.w) - x,
                            max(w1.y + w1.h, w2.y + w2.h) - y,
                            max(0.66, (w1.conf + w2.conf) / 2.0)))
        return HeaderTemplateVerifier._nms_hits(hits)

    @staticmethod
    def _strip_has_ink(strip: "np.ndarray") -> bool:
        """Cheap pre-filter: skip OCR on strips that are blank or solid."""
        if strip.size == 0:
            return False
        dark = float(np.count_nonzero(strip < 128)) / strip.size
        return 0.01 <= dark <= 0.85

    # -- helpers --------------------------------------------------------------
    def _header_strip(self, gray: "np.ndarray",
                      rect: Tuple[int, int, int, int]) -> "np.ndarray":
        cfg = self._cfg
        x, y, w, h = rect
        strip_h = int(clamp(h * cfg.strip_frac_of_box,
                            cfg.strip_min_px, cfg.strip_max_px))
        inset = 4  # keep the border line itself out of the strip
        y0 = clamp(y + 2, 0, gray.shape[0] - 1)
        y1 = clamp(y + strip_h, y0 + 1, gray.shape[0])
        x0 = clamp(x + inset, 0, gray.shape[1] - 1)
        x1 = clamp(x + w - inset, x0 + 1, gray.shape[1])
        return gray[y0:y1, x0:x1]

    def _deduplicate(self, detections: List[Detection]) -> List[Detection]:
        cfg = self._cfg
        ordered = sorted(detections, key=lambda d: -d.score)
        kept: List[Detection] = []
        for det in ordered:
            duplicate = False
            for existing in kept:
                if rect_iou(det.rect, existing.rect) >= cfg.nms_iou_threshold:
                    duplicate = True
                    break
                if rect_containment(det.rect, existing.rect) >= \
                        cfg.nms_containment:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(det)
        # Reading order: top-to-bottom, then left-to-right.
        kept.sort(key=lambda d: (d.rect[1], d.rect[0]))
        return kept

    def crop(self, page_bgr: "np.ndarray",
             detection: Detection) -> "np.ndarray":
        """Crop a detection from the ORIGINAL full-resolution page."""
        pad = self._cfg.crop_padding
        x, y, w, h = detection.rect
        inv = 1.0 / self._scale
        x0 = int(clamp((x - pad) * inv, 0, page_bgr.shape[1] - 1))
        y0 = int(clamp((y - pad) * inv, 0, page_bgr.shape[0] - 1))
        x1 = int(clamp((x + w + pad) * inv, x0 + 1, page_bgr.shape[1]))
        y1 = int(clamp((y + h + pad) * inv, y0 + 1, page_bgr.shape[0]))
        return page_bgr[y0:y1, x0:x1].copy()


# --- Per-newspaper pipelines -------------------------------------------------
# Each newspaper gets its own pipeline class with its own templates and
# thresholds, so tuning one paper never affects another.











# =============================================================================
# 8. EXTRACTORS (BASE CLASS, GUJARAT SAMACHAR, SANDESH, DIVYA BHASKAR)
# =============================================================================

class BaseNewspaperExtractor:
    """Contract + shared workflow for newspaper-specific extractors.

    Adding a newspaper = subclass this, implement matches() / build_url() /
    edition_from_url() / discover() / fetch_page(), then register the class
    in NEWSPAPER_REGISTRY.  The extraction loop itself (download -> detect ->
    crop -> report) is shared; nothing in the GUI changes.
    """

    display_name: str = "Unknown"
    default_edition: str = "ahmedabad"
    #: how many days back the paper's online archive reaches (None = any date)
    days_back_limit: Optional[int] = None
    #: this newspaper's own detection pipeline class
    pipeline_cls: Type[NoticeDetectionPipeline] = NoticeDetectionPipeline
    #: known edition slugs; when non-empty the GUI shows an Edition dropdown
    editions: Tuple[str, ...] = ()
    #: editions the loop features run ("All editions" checkbox / "Extract
    #: All" button); empty falls back to `editions`, else the default.
    loop_editions: Tuple[str, ...] = ()
    #: extra warning logged when a run ends with pages but ZERO notices
    zero_results_hint: str = ""
    #: save the first downloaded pages + a score report when a run ends
    #: with ZERO notices (used to tune detection for a new newspaper)
    debug_on_zero: bool = False
    #: ISO date of the edition being extracted (stamped onto results)
    current_issue_date: str = ""

    def __init__(self, broad: bool = False):
        # broad=True widens matching to all legal-notice headers (UI toggle).
        self.broad = broad

    # -- URL / date helpers (used by the GUI's date picker) -------------------
    @classmethod
    def matches(cls, url: str) -> bool:
        raise NotImplementedError

    @classmethod
    def build_url(cls, edition: str, day: "date") -> str:
        raise NotImplementedError

    @classmethod
    def edition_from_url(cls, url: str) -> Optional[str]:
        return None

    @classmethod
    def min_date(cls) -> "date":
        if cls.days_back_limit is None:
            return date(2000, 1, 1)
        return date.today() - timedelta(days=cls.days_back_limit - 1)

    @classmethod
    def get_loop_editions(cls) -> Tuple[str, ...]:
        """Editions to run when looping this newspaper."""
        return cls.loop_editions or cls.editions or (cls.default_edition,)

    # -- newspaper-specific hooks --------------------------------------------
    def discover(self, downloader: PageDownloader, url: str,
                 reporter: ProgressReporter) -> List[PageRef]:
        raise NotImplementedError

    def fetch_page(self, downloader: PageDownloader, page: PageRef,
                   reporter: ProgressReporter) -> "np.ndarray":
        raise NotImplementedError

    # -- zero-result diagnostics ---------------------------------------------
    def _write_zero_debug(self, debug_pages, debug_report, ocr_engine,
                          pipeline, reporter) -> Optional[str]:
        """Save the first downloaded pages plus a candidate-score report
        next to the program, so detection can be tuned offline."""
        try:
            folder = config.debug_dir(
                self.display_name.lower().replace(" ", "_"))
            for page_no, image in debug_pages:
                save_image_unicode(
                    image, os.path.join(folder, f"page_{page_no:02d}.png"))
            lines = [
                APP_TITLE,
                f"newspaper: {self.display_name}",
                "ocr: " + (ocr_engine.name if ocr_engine else "NONE"),
                "ocr_gujarati: " + str(bool(
                    ocr_engine and ocr_engine.supports_gujarati)),
                "templates: " + str(len(pipeline.verifier.templates)),
                "font_positive_templates: " + str(len(
                    pipeline.verifier.font_positive_templates)),
                "box_match_threshold: " + str(
                    pipeline._cfg.box_match_threshold),
                "",
            ] + debug_report
            with open(os.path.join(folder, "report.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("\n".join(lines))
            return folder
        except Exception as exc:
            reporter.log(f"Could not save diagnostics: {exc}", "warn")
            return None

    # -- shared workflow ------------------------------------------------------
    def extract(self, url: str, reporter: ProgressReporter) -> None:
        """Run the full pipeline for ONE URL; report via `reporter`."""
        self.extract_all([("", url)], reporter)

    def extract_all(self, jobs: List[Tuple[str, str]],
                    reporter: ProgressReporter,
                    finalize: bool = True,
                    start_result_id: int = 0
                    ) -> Tuple[int, int, int, List[str]]:
        """Run the pipeline for several (edition_label, url) jobs in one
        session, feeding one shared gallery (v1.9 loop feature).

        finalize=False suppresses the terminal done()/failed() messages and
        re-raises errors so a caller can chain several newspapers; returns
        (last_result_id, found_total, pages_total, per_edition_notes)."""
        downloader = PageDownloader(reporter)
        result_id = start_result_id
        found_total = 0
        pages_total = 0
        edition_notes: List[str] = []
        try:
            # select_ocr_engine() logs the backend chain once per process;
            # here we only note what this agent ended up with.
            ocr_engine = select_ocr_engine(reporter)
            if ocr_engine is not None and ocr_engine.supports_gujarati:
                reporter.log(f"[OCR] {self.display_name} -> "
                             f"Using {ocr_engine.name} (Gujarati), "
                             f"{OCR_WORKERS} readers in parallel", "success")
            else:
                reporter.log(f"[Warning] {self.display_name} -> no Gujarati "
                             "OCR, falling back to template detection",
                             "warn")
            pipeline = self.pipeline_cls(
                reporter=reporter,
                ocr_engine=ocr_engine, broad=self.broad)
            if not pipeline.usable:
                raise ExtractionError(
                    "Cannot verify notice headers: the built-in templates "
                    "failed to load and no OCR engine is available.  "
                    "Reinstall the application file.")
            if not (pipeline.verifier.gujarati_capable or
                    (ocr_engine and ocr_engine.supports_gujarati)):
                reporter.log(
                    "WARNING: no Gujarati matching capability is active - "
                    "detection quality will be poor.", "error")

            multi = len(jobs) > 1
            debug_pages: List[Tuple[int, "np.ndarray"]] = []
            debug_report: List[str] = []
            for job_index, (label, job_url) in enumerate(jobs, start=1):
                reporter.check_cancel()
                prefix = f"[{label}] " if label else ""
                if multi:
                    reporter.separator()
                    reporter.log(f"=====  Edition {job_index}/{len(jobs)}: "
                                 f"{label or job_url}  =====")
                reporter.phase(f"{prefix}Opening edition...")
                reporter.log("Opening Edition...")
                try:
                    pages = self.discover(downloader, job_url, reporter)
                except ExtractionCancelled:
                    raise
                except ExtractionError as exc:
                    if not multi and finalize:
                        raise
                    reporter.log(f"Edition '{label or job_url}' skipped: "
                                 f"{exc}", "error")
                    edition_notes.append(f"{label or 'edition'}: unavailable")
                    continue

                if PAGE_LIMIT[0]:
                    pages = pages[:PAGE_LIMIT[0]]
                    reporter.log(f"  (page limit: first {len(pages)})", "dim")
                total = len(pages)
                pages_total += total
                reporter.progress(0, total)
                found_here = 0
                failed_pages: List[int] = []

                # Pages are downloaded a few ahead of the detector so the
                # network and the CPU work at the same time.
                pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=PAGE_PREFETCH,
                    thread_name_prefix="page-fetch")
                futures: Dict[int, "concurrent.futures.Future"] = {}
                next_submit = 0

                def _pump() -> None:
                    nonlocal next_submit
                    while (next_submit < len(pages)
                           and len(futures) < PAGE_PREFETCH):
                        target = pages[next_submit]
                        futures[next_submit] = pool.submit(
                            self.fetch_page, downloader, target, reporter)
                        next_submit += 1

                try:
                  _pump()
                  for index, page in enumerate(pages):
                    reporter.check_cancel()
                    reporter.separator()
                    reporter.log(f"{prefix}Page {page.page_number} / {total}")
                    reporter.phase(f"{prefix}Page {page.page_number}"
                                   f" / {total}")

                    # -- Download (already running in the background) --------
                    try:
                        future = futures.pop(index, None)
                        if future is None:
                            image = self.fetch_page(downloader, page,
                                                    reporter)
                        else:
                            image = future.result()
                        _pump()
                    except ExtractionError as exc:
                        if str(exc).startswith("AUTH:"):
                            # One clear message, then stop - grinding
                            # through 20 identical auth failures helps
                            # nobody.
                            reporter.log(str(exc)[5:].strip(), "error")
                            raise ExtractionError(
                                f"{self.display_name}: cannot download the "
                                "pages - see the explanation above.")
                        reporter.log(f"  SKIPPED - {exc}", "error")
                        failed_pages.append(page.page_number)
                        if self.debug_on_zero and found_total == 0:
                            debug_report.append(
                                f"page {page.page_number}: SKIPPED "
                                f"({exc})  src={page.image_url}")
                        reporter.progress(page.page_number, total)
                        continue
                    reporter.log(f"  Got {image.shape[1]}x{image.shape[0]} px")

                    # -- Detect -----------------------------------------------
                    reporter.log("  Searching for Public Notices...")
                    try:
                        detections = pipeline.detect(image)
                    except ExtractionCancelled:
                        raise
                    except Exception as exc:
                        reporter.log(f"  Detection error: {exc}", "error")
                        failed_pages.append(page.page_number)
                        reporter.progress(page.page_number, total)
                        continue

                    if self.debug_on_zero and found_total == 0:
                        if len(debug_pages) < 3:
                            debug_pages.append((page.page_number, image))
                        top = sorted(pipeline.last_candidate_scores,
                                     reverse=True)[:5]
                        debug_report.append(
                            f"page {page.page_number}: "
                            f"{image.shape[1]}x{image.shape[0]}px  "
                            f"boxes={len(pipeline.last_candidate_scores)}  "
                            f"top_scores={top}  found={len(detections)}  "
                            f"src={page.image_url}")

                    # -- Crop & report ----------------------------------------
                    if not detections:
                        reporter.log("  No Public Notices Found", "dim")
                    else:
                        count = len(detections)
                        reporter.log(f"  {count} Public Notice"
                                     f"{'s' if count != 1 else ''} Found",
                                     "success")
                        reporter.log("  Cropping...")
                        for idx, det in enumerate(detections, start=1):
                            crop = pipeline.crop(image, det)
                            result_id += 1
                            found_total += 1
                            found_here += 1
                            reporter.result(NoticeResult(
                                result_id=result_id,
                                page_number=page.page_number,
                                index_on_page=idx,
                                image_bgr=crop,
                                confidence=int(clamp(round(det.score * 100),
                                                     1, 99)),
                                method=det.method,
                                edition=label,
                                newspaper=self.display_name,
                                issue_date=getattr(self, "current_issue_date",
                                                   ""),
                            ))
                    reporter.progress(page.page_number, total)
                finally:
                    for pending in futures.values():
                        pending.cancel()
                    pool.shutdown(wait=False)

                note = f"{label or 'edition'}: {found_here}"
                if failed_pages:
                    note += f" (page errors: {failed_pages})"
                edition_notes.append(note)

            if finalize:
                reporter.separator()
                if found_total == 0 and pages_total > 0:
                    if self.debug_on_zero and (debug_pages or
                                               debug_report):
                        folder = self._write_zero_debug(
                            debug_pages, debug_report, ocr_engine,
                            pipeline, reporter)
                        if folder:
                            reporter.log("Diagnostic pages + report saved "
                                         f"to: {folder}", "warn")
                    if ocr_engine is None or \
                            not ocr_engine.supports_gujarati:
                        reporter.log(
                            "Tip: zero notices with NO Gujarati OCR active "
                            "usually means the OCR backend, not the page.  "
                            "Run  python setup_ocr.py  to install one "
                            "(Windows OCR pack or Tesseract + guj).", "warn")
                    if self.zero_results_hint:
                        reporter.log(self.zero_results_hint, "warn")
                summary = (f"Finished: {found_total} Public Notice"
                           f"{'s' if found_total != 1 else ''} found "
                           f"across {pages_total} pages.")
                if multi:
                    summary += "  [" + "; ".join(edition_notes) + "]"
                elif edition_notes and "page errors" in edition_notes[0]:
                    summary += "  (" + edition_notes[0].split("(", 1)[1]
                reporter.log(summary,
                             "success" if found_total else "info")
                reporter.done(summary)
            return result_id, found_total, pages_total, edition_notes

        except ExtractionCancelled:
            if not finalize:
                raise
            reporter.log("Extraction cancelled by user.", "warn")
            reporter.cancelled()
            return result_id, found_total, pages_total, edition_notes
        except ExtractionError as exc:
            if not finalize:
                raise
            reporter.log(str(exc), "error")
            reporter.failed(str(exc))
            return result_id, found_total, pages_total, edition_notes
        except Exception:
            if not finalize:
                raise
            reporter.log("Unexpected error:\n" + traceback.format_exc(),
                         "error")
            reporter.failed("An unexpected error occurred - see the log.")
            return result_id, found_total, pages_total, edition_notes
        finally:
            downloader.cleanup()












# The registry.  Filled in at startup from the 'newspapers' package - one
# module per newspaper - so adding a paper means adding a file, never editing
# this one.  See scrapers/__init__.py.
#: {display name: extractor class}, in the order the GUI should list them.
NEWSPAPER_REGISTRY: Dict[str, Type[BaseNewspaperExtractor]] = {}


def register_newspapers(
        mapping: Dict[str, Type[BaseNewspaperExtractor]]) -> None:
    """Publish the loaded newspaper modules to the GUI."""
    NEWSPAPER_REGISTRY.update(mapping)


def find_extractor_for_url(url: str) -> Optional[Type[BaseNewspaperExtractor]]:
    """The extractor that recognises `url`, or None."""
    for cls in NEWSPAPER_REGISTRY.values():
        try:
            if cls.matches(url):
                return cls
        except Exception:
            continue
    return None


def newspaper_module(name: str):
    """A newspaper plugin module, imported on demand.

    The Tools menu drives Divya Bhaskar's login and the Sandesh probe, which
    live in those plugins.  Importing them lazily here keeps the dependency
    one-way (plugins import the core, never the reverse)."""
    import importlib
    return importlib.import_module(f"{__package__}.scrapers.{name}")


def extractor_named(display_name: str
                    ) -> Optional[Type[BaseNewspaperExtractor]]:
    """Look an extractor up by its display name."""
    return NEWSPAPER_REGISTRY.get(display_name)


#: Display name of the local-PDF plugin; the GUI treats it specially (it is
#: excluded from an "All Newspapers" sweep and drives the Open PDF button).
LOCAL_PDF_NAME = "PDF File"


def local_pdf_extractor() -> Optional[Type[BaseNewspaperExtractor]]:
    """The local-PDF extractor, or None when that plugin is absent."""
    return NEWSPAPER_REGISTRY.get(LOCAL_PDF_NAME)


# =============================================================================
# 9. GUI
# =============================================================================

#: StatusLogPanel lives in ui/app.py (imported at the top of this file).


class ImagePreviewPanel(ttk.LabelFrame):
    """Preview with zoom / scroll / fit width / fit height / save."""

    def __init__(self, master, title="Image Preview"):
        super().__init__(master, text=title)
        self._pil_image: Optional["Image.Image"] = None
        self._result: Optional[NoticeResult] = None
        self._zoom = 1.0
        self._fit_mode: Optional[str] = "fit_width"
        self._photo = None  # keep a reference or Tk garbage-collects it

        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew",
                     padx=6, pady=(6, 2))
        self._btns = {}
        for key, label, cmd in (
                ("zoom_in", "Zoom In", self.zoom_in),
                ("zoom_out", "Zoom Out", self.zoom_out),
                ("actual", "100%", self.zoom_actual),
                ("fit_w", "Fit Width", self.fit_width),
                ("fit_h", "Fit Height", self.fit_height),
                ("save", "Save Image...", self.save_current)):
            btn = ttk.Button(toolbar, text=label, command=cmd, width=11)
            btn.pack(side="left", padx=(0, 4))
            self._btns[key] = btn
        self._zoom_label = ttk.Label(toolbar, text="")
        self._zoom_label.pack(side="right")

        self._canvas = tk.Canvas(self, background="#808080",
                                 highlightthickness=0, relief="sunken",
                                 borderwidth=1)
        hbar = ttk.Scrollbar(self, orient="horizontal",
                             command=self._canvas.xview)
        vbar = ttk.Scrollbar(self, orient="vertical",
                             command=self._canvas.yview)
        self._canvas.configure(xscrollcommand=hbar.set,
                               yscrollcommand=vbar.set)
        self._canvas.grid(row=1, column=0, sticky="nsew", padx=(6, 0))
        vbar.grid(row=1, column=1, sticky="ns", padx=(0, 6))
        hbar.grid(row=2, column=0, sticky="ew", padx=(6, 0), pady=(0, 6))
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self._canvas.bind("<Configure>", self._on_canvas_resized)
        # Mouse-wheel scrolling/zooming is routed by the application-wide
        # handler (Application._on_global_wheel) so it works no matter which
        # child widget is under the pointer.
        self._set_buttons_enabled(False)

    # -- public API -----------------------------------------------------------
    def show_result(self, result: NoticeResult) -> None:
        self._result = result
        self._pil_image = bgr_to_pil(result.image_bgr)
        self.configure(text=f"Image Preview - {result.caption}")
        self._fit_mode = "fit_width"
        self._set_buttons_enabled(True)
        self._render()

    def clear(self) -> None:
        self._result = None
        self._pil_image = None
        self._canvas.delete("all")
        self.configure(text="Image Preview")
        self._zoom_label.configure(text="")
        self._set_buttons_enabled(False)

    # -- zoom controls --------------------------------------------------------
    def zoom_in(self):
        self._apply_zoom(self._zoom * PREVIEW_ZOOM_STEP)

    def zoom_out(self):
        self._apply_zoom(self._zoom / PREVIEW_ZOOM_STEP)

    def zoom_actual(self):
        self._apply_zoom(1.0)

    def fit_width(self):
        self._fit_mode = "fit_width"
        self._render()

    def fit_height(self):
        self._fit_mode = "fit_height"
        self._render()

    def save_current(self):
        if self._result is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Save Notice Image",
            defaultextension=".png",
            initialfile=self._result.suggested_filename,
            filetypes=[("PNG image", "*.png"), ("JPEG image", "*.jpg"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            save_image_unicode(self._result.image_bgr, path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not save image:\n{exc}",
                                 parent=self)

    # -- internals ------------------------------------------------------------
    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for btn in self._btns.values():
            btn.configure(state=state)

    def _apply_zoom(self, zoom: float) -> None:
        if self._pil_image is None:
            return
        self._fit_mode = None
        self._zoom = clamp(zoom, PREVIEW_MIN_ZOOM, PREVIEW_MAX_ZOOM)
        self._render()

    def _effective_zoom(self) -> float:
        if self._pil_image is None:
            return 1.0
        cw = max(1, self._canvas.winfo_width())
        ch = max(1, self._canvas.winfo_height())
        iw, ih = self._pil_image.size
        if self._fit_mode == "fit_width":
            return clamp(cw / iw, PREVIEW_MIN_ZOOM, PREVIEW_MAX_ZOOM)
        if self._fit_mode == "fit_height":
            return clamp(ch / ih, PREVIEW_MIN_ZOOM, PREVIEW_MAX_ZOOM)
        return self._zoom

    def _render(self) -> None:
        if self._pil_image is None:
            return
        zoom = self._effective_zoom()
        self._zoom = zoom
        iw, ih = self._pil_image.size
        tw = clamp(int(iw * zoom), 1, PREVIEW_MAX_RENDER_DIM)
        th = clamp(int(ih * zoom), 1, PREVIEW_MAX_RENDER_DIM)
        resampler = Image.LANCZOS if zoom < 1.0 else Image.BICUBIC
        try:
            resized = self._pil_image.resize((tw, th), resampler)
        except (MemoryError, ValueError):
            return
        self._photo = ImageTk.PhotoImage(resized)
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, image=self._photo, anchor="nw")
        self._canvas.configure(scrollregion=(0, 0, tw, th))
        self._zoom_label.configure(text=f"{int(round(zoom * 100))}%")

    def _on_canvas_resized(self, _event) -> None:
        if self._fit_mode:
            self._render()

    def _on_mousewheel(self, event) -> None:
        delta = -1 if getattr(event, "delta", 0) > 0 or \
            getattr(event, "num", 0) == 4 else 1
        self._canvas.yview_scroll(delta * 2, "units")

    def _on_ctrl_wheel(self, event) -> None:
        up = getattr(event, "delta", 0) > 0 or getattr(event, "num", 0) == 4
        (self.zoom_in if up else self.zoom_out)()


class PreviewWindow(tk.Toplevel):
    """A standalone preview window (opened from a gallery card)."""

    def __init__(self, master, result: NoticeResult):
        super().__init__(master)
        self.title(f"Notice Preview - {result.suggested_filename}")
        self.geometry("760x620")
        self.minsize(420, 320)
        panel = ImagePreviewPanel(self, title="Notice")
        panel.pack(fill="both", expand=True, padx=6, pady=6)
        panel.show_result(result)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.transient(master.winfo_toplevel())


class NoticeCard(ttk.Frame):
    """One gallery entry: thumbnail, caption, select-checkbox, buttons."""

    def __init__(self, master, result: NoticeResult,
                 on_open: Callable[[NoticeResult], None],
                 on_save: Callable[[NoticeResult], None],
                 on_click: Callable[[NoticeResult], None]):
        matched = bool(result.match_boxes)
        super().__init__(master, relief="solid" if matched else "groove",
                         borderwidth=3 if matched else 2, padding=6,
                         style="Match.TFrame" if matched else "TFrame")
        self.result = result
        self.selected = tk.BooleanVar(value=True)

        pil = bgr_to_pil(result.image_bgr)
        scale = GALLERY_THUMB_WIDTH / max(1, pil.width)
        thumb_h = max(1, int(pil.height * scale))
        thumb = pil.resize((GALLERY_THUMB_WIDTH, thumb_h), Image.LANCZOS)
        if matched:
            # Paint the hit the way a text selection looks: a translucent
            # blue wash over the word, drawn on the thumbnail so the box
            # lines up with what the user is actually looking at.
            thumb = thumb.convert("RGB")
            wash = Image.new("RGBA", thumb.size, (0, 0, 0, 0))
            pen = ImageDraw.Draw(wash)
            for bx, by, bw, bh in result.match_boxes:
                box = (int(bx * scale) - 2, int(by * scale) - 2,
                       int((bx + bw) * scale) + 2, int((by + bh) * scale) + 2)
                pen.rectangle(box, fill=(37, 99, 235, 90),
                              outline=(37, 99, 235, 255), width=2)
            thumb = Image.alpha_composite(thumb.convert("RGBA"),
                                          wash).convert("RGB")
        self._photo = ImageTk.PhotoImage(thumb)

        img_label = ttk.Label(self, image=self._photo, cursor="hand2")
        img_label.grid(row=0, column=0, columnspan=3)
        img_label.bind("<Button-1>", lambda _e: on_click(result))
        img_label.bind("<Double-Button-1>", lambda _e: on_open(result))

        title = ttk.Label(self, text=f"Notice {result.result_id}",
                          font=("Segoe UI", 9, "bold"))
        title.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        meta = ttk.Label(self, text=result.caption, foreground="#555555")
        meta.grid(row=2, column=0, columnspan=3, sticky="w")

        check = ttk.Checkbutton(self, text="Select", variable=self.selected)
        check.grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Button(self, text="Open", width=6,
                   command=lambda: on_open(result)
                   ).grid(row=3, column=1, sticky="e", pady=(4, 0), padx=2)
        ttk.Button(self, text="Save", width=6,
                   command=lambda: on_save(result)
                   ).grid(row=3, column=2, sticky="e", pady=(4, 0))


class GalleryPanel(ttk.LabelFrame):
    """Scrollable masonry of NoticeCards, paginated per newspaper.

    Results arrive grouped by section (one heading per newspaper / edition /
    date).  Exactly ONE section is on screen at a time - Previous / Next (or
    the jump list) move between papers.  Sections larger than
    GALLERY_PAGE_SIZE are split into numbered screens, which also keeps
    every screen far below Tk's canvas addressing limit.
    """

    def __init__(self, master, on_open, on_save, on_click):
        super().__init__(master, text="Detected Public Notices")
        self._on_open = on_open
        self._on_save = on_save
        self._on_click = on_click
        #: [{"title": str, "results": [NoticeResult]}]
        self.sections: List[Dict[str, object]] = []
        #: EVERY notice from the whole run
        self.results: List[NoticeResult] = []
        #: cards currently on screen
        self.cards: List[NoticeCard] = []
        self._heading_widget: Optional[tk.Misc] = None
        self._page_index = 0
        self._pages_cache: List[Tuple[int, int]] = []
        #: while a run is going, follow the newest section automatically
        self.follow_live = True
        self._deselected: set = set()
        self._columns = 2
        self._laying_out = False
        self.on_show_more: Optional[Callable[[], None]] = None   # legacy
        self.on_page_change: Optional[Callable[[], None]] = None

        # ---- navigation bar -------------------------------------------------
        nav = ttk.Frame(self)
        nav.grid(row=0, column=0, columnspan=2, sticky="ew",
                 padx=6, pady=(6, 0))
        self._prev_btn = ttk.Button(nav, text="<< Previous", width=13,
                                    command=self.prev_page, state="disabled")
        self._prev_btn.pack(side="left")
        self._next_btn = ttk.Button(nav, text="Next >>", width=13,
                                    command=self.next_page, state="disabled")
        self._next_btn.pack(side="right")
        self._page_var = tk.StringVar()
        self._page_combo = ttk.Combobox(nav, textvariable=self._page_var,
                                        state="readonly", width=40)
        self._page_combo.pack(side="left", fill="x", expand=True, padx=6)
        self._page_combo.bind("<<ComboboxSelected>>", self._on_combo_pick)

        # Search state.  The widgets live in the TOP control bar (under the
        # dates) - the Application calls build_search_bar() to put them there.
        self.search_var = tk.StringVar()
        self._search_only = tk.BooleanVar(value=False)
        self._search_btn: Optional[ttk.Button] = None
        self._search_label: Optional[ttk.Label] = None
        #: set by the Application - runs the OCR + matching off the UI thread
        self.on_search: Optional[Callable[[str], None]] = None

        # ---- scrolling canvas ----------------------------------------------
        self._canvas = tk.Canvas(self, highlightthickness=0,
                                 background="#f0f0f0")
        vbar = ttk.Scrollbar(self, orient="vertical",
                             command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vbar.set)
        self._canvas.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=6)
        vbar.grid(row=1, column=1, sticky="ns", padx=(0, 6), pady=6)
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self._inner = ttk.Frame(self._canvas)
        self._window = self._canvas.create_window((0, 0), window=self._inner,
                                                  anchor="nw")
        self._canvas.bind("<Configure>", self._on_canvas_resize)

        self._empty_label = ttk.Label(
            self._inner, foreground="#767676", justify="center",
            text="No notices yet.\n\nChoose a newspaper and date above, "
                 "then press Extract.")
        self._empty_label.grid(row=0, column=0, padx=20, pady=30)

    # -- public API -----------------------------------------------------------
    def clear(self) -> None:
        self._destroy_page_widgets()
        self.sections = []
        self.results = []
        self._deselected = set()
        self._page_index = 0
        self._pages_cache = []
        self.follow_live = True
        # Tk 9 rejects height="" as a reset, so the window item is recreated.
        self._canvas.delete(self._window)
        self._window = self._canvas.create_window(
            (0, 0), window=self._inner, anchor="nw",
            width=max(1, self._canvas.winfo_width()))
        self._canvas.configure(scrollregion=(0, 0, 0, 0))
        self._canvas.yview_moveto(0.0)
        self._empty_label.grid(row=0, column=0, padx=20, pady=30)
        self._update_nav()

    def add_heading(self, text: str) -> None:
        """Start a new section (one newspaper / edition / date)."""
        self._section_for(text)

    def _section_for(self, title: str) -> int:
        """Index of the section called `title`, creating it if needed.

        Find-or-create (rather than always-append) is what lets several
        edition agents stream notices at the same time: a notice from the
        third newspaper does not get filed under the second one just because
        that heading arrived most recently."""
        for index, section in enumerate(self.sections):
            if section["title"] == title:
                return index
        self.sections.append({"title": title, "results": []})
        self._pages_cache = []
        # No auto-jump on creation: the driver registers every edition's
        # heading up front, and jumping to each in turn would leave the user
        # staring at the last paper before a single notice exists.
        self._update_nav()
        return len(self.sections) - 1

    def add_result(self, result: NoticeResult) -> None:
        title = result.section_title
        if title:
            index = self._section_for(title)
        elif self.sections:
            index = len(self.sections) - 1
        else:
            index = self._section_for("Results")
        section = self.sections[index]
        section["results"].append(result)      # type: ignore[union-attr]
        self.results.append(result)
        self._pages_cache = []
        target = self._page_of(index,
                               len(section["results"]) - 1)  # type: ignore
        if target == self._page_index:
            self._append_card(result)          # cheap: no full redraw
            self._update_nav()
        elif self.follow_live and len(self.results) == 1:
            # Jump once, to the very first notice of the run, so something
            # visibly lands.  After that the view stays put: agents publish
            # concurrently, and chasing every arrival would yank the page
            # between newspapers while the user is trying to read one.
            self.goto_page(target, user=False)
        else:
            self._update_nav()

    # -- search ---------------------------------------------------------------
    def build_search_bar(self, parent) -> ttk.Frame:
        """The Find-text bar, built into `parent` (the top control bar, so it
        sits right under the dates).  English and Gujarati both work - the
        entry is given the Gujarati-capable UI font."""
        bar = ttk.Frame(parent)
        ttk.Label(bar, text="Find text:").pack(side="left")
        entry = ttk.Entry(bar, textvariable=self.search_var, width=30,
                          font=(find_gujarati_ui_font(self.winfo_toplevel()),
                                10))
        entry.pack(side="left", padx=(4, 4))
        entry.bind("<Return>", lambda _e: self._fire_search())
        self._search_btn = ttk.Button(bar, text="Search", width=9,
                                      command=self._fire_search)
        self._search_btn.pack(side="left")
        ttk.Button(bar, text="Clear", width=7,
                   command=self.clear_search).pack(side="left", padx=(4, 0))
        ttk.Checkbutton(bar, text="Show only matches",
                        variable=self._search_only,
                        command=self._refresh_after_search).pack(
            side="left", padx=(10, 0))
        self._search_label = ttk.Label(bar, text="", foreground="#2563eb")
        self._search_label.pack(side="left", padx=(10, 0))
        return bar

    def _fire_search(self) -> None:
        query = self.search_var.get().strip()
        if not query:
            self.clear_search()
            return
        if self.on_search is not None:
            if self._search_btn is not None:
                self._search_btn.configure(state="disabled")
            if self._search_label is not None:
                self._search_label.configure(text="searching...")
            self.on_search(query)

    def clear_search(self) -> None:
        """Drop the highlights and show every notice again."""
        self.search_var.set("")
        for result in self.results:
            result.match_boxes = []
        if self._search_label is not None:
            self._search_label.configure(text="")
        if self._search_btn is not None:
            self._search_btn.configure(state="normal")
        self._refresh_after_search()

    def search_finished(self, query: str, matched: int, scanned: int) -> None:
        """Called on the Tk thread once the background OCR + match is done."""
        if self._search_btn is not None:
            self._search_btn.configure(state="normal")
        if self._search_label is None:
            pass
        elif matched:
            self._search_label.configure(
                text=f"{matched} of {scanned} notices contain "
                     f"“{query}”")
        else:
            self._search_label.configure(
                text=f"no match for “{query}” in {scanned} notices")
        self._refresh_after_search()
        # Land on the first paper that actually has a hit.
        for index, section in enumerate(self.sections):
            if any(r.match_boxes for r in section["results"]):  # type: ignore
                self.goto_page(self._page_of(index, 0), user=False)
                break

    def _refresh_after_search(self) -> None:
        self._pages_cache = []
        self._page_index = 0
        self._render_page()

    def has_search(self) -> bool:
        return bool(self.search_var.get().strip())

    def selected_results(self) -> List[NoticeResult]:
        """Ticked notices across EVERY page (un-ticking is remembered when
        you move between papers)."""
        self._harvest_selection()
        return [r for r in self.results if id(r) not in self._deselected]

    def all_results(self) -> List[NoticeResult]:
        return list(self.results)

    def pending_count(self) -> int:
        """Notices that exist but are not on the current screen."""
        return max(0, len(self.results) - len(self.cards))

    def page_label(self) -> str:
        pages = self._page_list()
        if not pages:
            return ""
        section_index, chunk = pages[self._page_index]
        section = self.sections[section_index]
        total_chunks = self._chunk_count(section_index)
        title = str(section["title"])
        if total_chunks > 1:
            title += f"   (part {chunk + 1}/{total_chunks})"
        return (f"{title}   -   screen {self._page_index + 1} of "
                f"{len(pages)}")

    # -- navigation -----------------------------------------------------------
    def next_page(self) -> None:
        self.goto_page(self._page_index + 1)

    def prev_page(self) -> None:
        self.goto_page(self._page_index - 1)

    def goto_page(self, index: int, user: bool = True) -> None:
        pages = self._page_list()
        if not pages:
            return
        index = int(clamp(index, 0, len(pages) - 1))
        if user:
            self.follow_live = False       # stop auto-jumping mid-run
        self._page_index = index
        self._render_page()
        if self.on_page_change is not None:
            self.on_page_change()

    def _on_combo_pick(self, _event=None) -> None:
        choice = self._page_var.get()
        options = self._combo_options()
        if choice in options:
            self.goto_page(options.index(choice))

    # -- paging model ---------------------------------------------------------
    def _visible_results(self, section_index: int) -> List[NoticeResult]:
        """A section's notices, honouring the 'show only matches' filter."""
        results = list(self.sections[section_index]["results"])  # type: ignore
        if self._search_only.get() and self.has_search():
            return [r for r in results if r.match_boxes]
        return results

    def _chunk_count(self, section_index: int) -> int:
        results = self._visible_results(section_index)
        return max(1, (len(results) + GALLERY_PAGE_SIZE - 1)
                   // GALLERY_PAGE_SIZE)

    def _page_list(self) -> List[Tuple[int, int]]:
        if not self._pages_cache:
            pages: List[Tuple[int, int]] = []
            for index in range(len(self.sections)):
                for chunk in range(self._chunk_count(index)):
                    pages.append((index, chunk))
            self._pages_cache = pages
        return self._pages_cache

    def _page_of(self, section_index: int, result_index: int) -> int:
        chunk = result_index // GALLERY_PAGE_SIZE
        for position, (si, ci) in enumerate(self._page_list()):
            if si == section_index and ci == chunk:
                return position
        return 0

    def _current_results(self) -> List[NoticeResult]:
        pages = self._page_list()
        if not pages:
            return []
        section_index, chunk = pages[self._page_index]
        results = self._visible_results(section_index)
        start = chunk * GALLERY_PAGE_SIZE
        return list(results[start:start + GALLERY_PAGE_SIZE])

    def _combo_options(self) -> List[str]:
        options: List[str] = []
        for section_index, chunk in self._page_list():
            section = self.sections[section_index]
            count = len(self._visible_results(section_index))
            total = self._chunk_count(section_index)
            label = f"{section['title']}  ({count})"
            if total > 1:
                label += f"  part {chunk + 1}/{total}"
            options.append(label)
        return options

    # -- rendering ------------------------------------------------------------
    def _destroy_page_widgets(self) -> None:
        self._harvest_selection()
        for card in self.cards:
            card.destroy()
        self.cards = []
        if self._heading_widget is not None:
            self._heading_widget.destroy()
            self._heading_widget = None

    def _harvest_selection(self) -> None:
        for card in self.cards:
            try:
                ticked = bool(card.selected.get())
            except Exception:
                ticked = True
            if ticked:
                self._deselected.discard(id(card.result))
            else:
                self._deselected.add(id(card.result))

    def _make_heading(self, text: str) -> None:
        holder = tk.Frame(self._inner, background="#20486e")
        holder._heading_text = text
        tk.Label(holder, text=text, background="#20486e",
                 foreground="#ffffff", anchor="w",
                 font=("Segoe UI", 11, "bold"), padx=10,
                 pady=6).pack(fill="both", expand=True)
        self._heading_widget = holder

    def _append_card(self, result: NoticeResult) -> None:
        card = NoticeCard(self._inner, result, self._on_open,
                          self._on_save, self._on_click)
        if id(result) in self._deselected:
            card.selected.set(False)
        self.cards.append(card)
        self._layout()

    def _render_page(self) -> None:
        self._destroy_page_widgets()
        pages = self._page_list()
        if not pages:
            self._empty_label.grid(row=0, column=0, padx=20, pady=30)
            self._update_nav()
            return
        self._empty_label.grid_forget()
        section_index, _chunk = pages[self._page_index]
        self._make_heading(self.page_label())
        for result in self._current_results():
            card = NoticeCard(self._inner, result, self._on_open,
                              self._on_save, self._on_click)
            if id(result) in self._deselected:
                card.selected.set(False)
            self.cards.append(card)
        self._canvas.yview_moveto(0.0)
        self._layout()
        self._update_nav()

    def _update_nav(self) -> None:
        pages = self._page_list()
        options = self._combo_options()
        self._page_combo.configure(values=options)
        if pages:
            self._page_var.set(options[min(self._page_index,
                                           len(options) - 1)])
            self._prev_btn.configure(
                state="normal" if self._page_index > 0 else "disabled")
            self._next_btn.configure(
                state="normal" if self._page_index < len(pages) - 1
                else "disabled")
            self.configure(text="Detected Public Notices  -  "
                                + self.page_label())
        else:
            self._page_var.set("")
            self._prev_btn.configure(state="disabled")
            self._next_btn.configure(state="disabled")
            self.configure(text="Detected Public Notices")

    def _layout(self) -> None:
        """Masonry: each card drops into the currently shortest column."""
        if self._laying_out:
            return
        self._laying_out = True
        try:
            self._inner.update_idletasks()
            gap = GALLERY_CARD_GAP
            card_w = max([c.winfo_reqwidth() for c in self.cards] or [220])
            avail = max(self._canvas.winfo_width(), card_w + 2 * gap)
            columns = max(GALLERY_MIN_COLUMNS,
                          (avail - gap) // (card_w + gap))
            self._columns = columns
            span = columns * (card_w + gap) + gap
            x_off = max(0, (avail - span) // 2)
            top = gap
            if self._heading_widget is not None:
                self._heading_widget.place(x=x_off + gap, y=top,
                                           width=max(200, span - gap))
                top += self._heading_widget.winfo_reqheight() + gap
            heights = [top] * columns
            for card in self.cards:
                col = min(range(columns), key=lambda c: heights[c])
                card.place(x=x_off + gap + col * (card_w + gap),
                           y=heights[col], width=card_w)
                heights[col] += card.winfo_reqheight() + gap
            total_h = max(heights) + gap
            self._canvas.itemconfigure(self._window, height=total_h)
            self._canvas.configure(scrollregion=(0, 0, avail, total_h))
        finally:
            self._laying_out = False

    def _on_canvas_resize(self, event) -> None:
        self._canvas.itemconfigure(self._window, width=event.width)
        self._layout()

    def _on_mousewheel(self, event) -> None:
        delta = -1 if getattr(event, "delta", 0) > 0 or \
            getattr(event, "num", 0) == 4 else 1
        self._canvas.yview_scroll(delta * 2, "units")


class DatePickerDialog(tk.Toplevel):
    """Small classic-style calendar popup (standard library only).  Days
    outside [min_date, max_date] are disabled - e.g. Gujarat Samachar's
    archive only covers the last 7 days."""

    def __init__(self, master, initial: "date", min_date: "date",
                 max_date: "date", on_pick: Callable[["date"], None],
                 note: str = ""):
        super().__init__(master)
        self.title("Choose Date")
        self._note = note
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self._on_pick = on_pick
        self._min = min_date
        self._max = max_date
        self._shown = date(initial.year, initial.month, 1)

        header = ttk.Frame(self, padding=(8, 8, 8, 0))
        header.pack(fill="x")
        self._prev_btn = ttk.Button(header, text="<", width=3,
                                    command=lambda: self._shift(-1))
        self._prev_btn.pack(side="left")
        self._title = ttk.Label(header, anchor="center",
                                font=("Segoe UI", 10, "bold"))
        self._title.pack(side="left", expand=True, fill="x")
        self._next_btn = ttk.Button(header, text=">", width=3,
                                    command=lambda: self._shift(1))
        self._next_btn.pack(side="right")

        self._grid = ttk.Frame(self, padding=8)
        self._grid.pack()
        if note:
            ttk.Label(self, text=note, wraplength=250, justify="left",
                      foreground="#7a4a00").pack(padx=10, pady=(0, 8))
        self._render()
        self.grab_set()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.update_idletasks()
        parent = master.winfo_toplevel()
        self.geometry(f"+{parent.winfo_rootx() + 140}"
                      f"+{parent.winfo_rooty() + 110}")

    def _shift(self, months: int) -> None:
        year, month = self._shown.year, self._shown.month + months
        year += (month - 1) // 12
        month = (month - 1) % 12 + 1
        self._shown = date(year, month, 1)
        self._render()

    def _render(self) -> None:
        for child in self._grid.winfo_children():
            child.destroy()
        self._title.configure(text=self._shown.strftime("%B %Y"))
        for col, name in enumerate(("Mo", "Tu", "We", "Th", "Fr",
                                    "Sa", "Su")):
            ttk.Label(self._grid, text=name, width=3, anchor="center",
                      foreground="#555555").grid(row=0, column=col,
                                                 padx=1, pady=(0, 3))
        weeks = _calendar.Calendar().monthdatescalendar(
            self._shown.year, self._shown.month)
        today = date.today()
        for row, week in enumerate(weeks, start=1):
            for col, day in enumerate(week):
                if day.month != self._shown.month:
                    ttk.Label(self._grid, text="", width=3).grid(
                        row=row, column=col)
                    continue
                state = "normal" if self._min <= day <= self._max \
                    else "disabled"
                text = f"[{day.day}]" if day == today else str(day.day)
                ttk.Button(self._grid, text=text, width=3, state=state,
                           command=lambda d=day: self._pick(d)
                           ).grid(row=row, column=col, padx=1, pady=1)
        shown_month = date(self._shown.year, self._shown.month, 1)
        self._prev_btn.configure(
            state="normal" if shown_month > date(self._min.year,
                                                 self._min.month, 1)
            else "disabled")
        self._next_btn.configure(
            state="normal" if shown_month < date(self._max.year,
                                                 self._max.month, 1)
            else "disabled")

    def _pick(self, day: "date") -> None:
        self.destroy()
        self._on_pick(day)


class AboutDialog(tk.Toplevel):
    """Custom About box.  Native message boxes cannot render Gujarati text
    reliably, so this uses an explicitly chosen Unicode-capable font."""

    def __init__(self, master, guj_font_family: str):
        super().__init__(master)
        self.title(f"About {APP_NAME}")
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())

        frame = ttk.Frame(self, padding=(18, 14))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=APP_TITLE,
                  font=(guj_font_family, 12, "bold")).pack(anchor="w")
        for text in (
                "Extracts Public Notices (જાહેર નોટિસ) from the "
                "Gujarat Samachar e-paper.",
                "Detection uses built-in visual templates cropped from real "
                "newspaper pages - no extra software required.",
                "Supported newspapers: Gujarat Samachar, Sandesh, "
                "Divya Bhaskar, Nav Gujarat Samay - plus any e-paper PDF "
                "file ('Open PDF...').",
                "Pick 'All Newspapers' and a From/To date range to sweep "
                "everything in one run.",
                "Tender (નિવિદા), auction (હરાજી) and recruitment boxes "
                "are filtered out automatically."):
            ttk.Label(frame, text=text, font=(guj_font_family, 10),
                      wraplength=400, justify="left").pack(
                anchor="w", pady=(8, 0))
        ttk.Button(frame, text="OK", width=10,
                   command=self.destroy).pack(pady=(16, 0))
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        # center over the parent window
        self.update_idletasks()
        parent = master.winfo_toplevel()
        x = parent.winfo_rootx() + (parent.winfo_width()
                                    - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height()
                                    - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")


class BufferedJobReporter:
    """A reporter for ONE edition agent inside a parallel run.

    Notices are published the moment they are cropped, each stamped with the
    section it belongs to, so the gallery fills up live while every other
    agent is still working.  `collected` is kept purely so the driver can
    report a per-edition count when the agent finishes.
    """

    def __init__(self, base: ProgressReporter, label: str,
                 section_title: str = ""):
        self._base = base
        self._label = label
        self._section_title = section_title or label
        self.collected: List[NoticeResult] = []

    # -- worker-facing API ----------------------------------------------------
    def check_cancel(self) -> None:
        self._base.check_cancel()

    def log(self, text: str, level: str = "info") -> None:
        stripped = text.strip()
        if not stripped or stripped.startswith("-" * 6):
            return                       # skip separators from parallel jobs
        self._base.log(f"[{self._label}] {stripped}", level)

    def separator(self) -> None:
        pass                             # too noisy when jobs interleave

    def phase(self, text: str) -> None:
        pass                             # the driver owns the phase label

    def progress(self, current: int, total: int) -> None:
        pass                             # the driver owns the progress bar

    def result(self, res: NoticeResult) -> None:
        res.section_title = self._section_title
        self.collected.append(res)
        self._base.result(res)           # straight to the gallery, live

    def heading(self, text: str) -> None:
        pass                             # the driver emits section headings

    def done(self, summary: str) -> None:
        pass

    def failed(self, message: str) -> None:
        pass

    def cancelled(self) -> None:
        pass


class _SilentReporter:
    """A no-op reporter for one-off cookie tests in dialogs."""
    def log(self, *args, **kwargs) -> None:
        pass

    def check_cancel(self) -> None:
        pass


class Application(ttk.Frame):
    """Main window: classic Windows utility layout."""

    def __init__(self, root: tk.Tk):
        super().__init__(root, padding=0)
        self.root = root
        self._worker: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._msg_queue: "queue.Queue" = queue.Queue()
        self._running = False
        self._installing = False
        self._selected_date = date.today()
        self._selected_date_to = date.today()
        self._paper_counts: Dict[str, int] = {}
        self._result_seq = 0
        self._searching = False
        self._guj_ui_font = find_gujarati_ui_font(root)

        root.title(APP_TITLE)
        root.geometry("1280x800")
        root.minsize(1000, 660)
        self._init_style()
        self._build_menu()
        self._build_ui()
        self.pack(fill="both", expand=True)
        self._install_global_wheel()
        self._poll_queue()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- style ----------------------------------------------------------------
    def _init_style(self) -> None:
        style = ttk.Style(self.root)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        default_font = tkfont.nametofont("TkDefaultFont")
        if sys.platform.startswith("win"):
            default_font.configure(family="Segoe UI", size=9)
        # Widgets that show Gujarati text use a Unicode-capable font.
        style.configure("Gujarati.TCheckbutton",
                        font=(self._guj_ui_font, 9))
        # The date fields are buttons that open the calendar, but they should
        # read as fields - left-aligned text, not a centred chunky button.
        style.configure("Date.TButton", anchor="w", padding=(6, 2))
        # A search hit reads like a text selection: blue surround on the card.
        style.configure("Match.TFrame", background="#2563eb")

    # -- menu -----------------------------------------------------------------
    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Save Selected...",
                              command=self.save_selected)
        file_menu.add_command(label="Save All...", command=self.save_all)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(
            label="Divya Bhaskar Auto-Login (from browser)...",
            command=self._open_db_autologin_dialog)
        tools_menu.add_command(label="Divya Bhaskar Login (paste cookie)...",
                               command=self._open_db_session_dialog)
        tools_menu.add_separator()
        tools_menu.add_command(label="Network (Proxy)...",
                               command=self._open_proxy_dialog)
        tools_menu.add_command(label="Download Dependencies...",
                               command=self.download_dependencies)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About...", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menubar)

    def _open_db_autologin_dialog(self) -> None:
        """Enable one-time automatic login: the app reads the CURRENT cookie
        from the chosen browser on every run, so nothing is pasted again."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Divya Bhaskar Auto-Login")
        dialog.geometry("640x460")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, justify="left", wraplength=600, text=(
            "Extract already signs in by itself - it drives a browser that "
            "keeps its own login.  This dialog is the older route, kept "
            "because it costs nothing and can seed that browser on a machine "
            "where you are already logged in.\n\n"
            "1.  In your browser, log in to www.divyabhaskar.co.in with "
            "your premium account and STAY logged in.\n"
            "2.  Pick that browser below and press 'Enable'.\n\n"
            "The app then reads that login straight from the browser's own "
            "cookie store, on every run.\n\n"
            "(Windows only.  Very new Chrome versions encrypt cookies so "
            "tightly that external apps cannot read them - that is exactly "
            "the case the automatic browser session handles instead.)"
        )).pack(anchor="w")

        current = newspaper_module("divya_bhaskar").db_load_autologin()
        def _pretty(cfg) -> str:
            b = cfg.get("browser", "")
            return "Automatic (any browser)" if b.lower() in ("auto", "") \
                else b
        if current and current.get("is_default"):
            status_txt = f"Currently ON - {_pretty(current)} (default)"
        elif current:
            status_txt = f"Currently ON - {_pretty(current)}"
        else:
            status_txt = "Currently OFF"
        status = ttk.Label(frame, text=status_txt,
                           font=("Segoe UI", 9, "bold"))
        status.pack(anchor="w", pady=(8, 4))

        row = ttk.Frame(frame)
        row.pack(anchor="w", pady=4)
        ttk.Label(row, text="Browser:").pack(side="left")
        browser_var = tk.StringVar()
        installed = newspaper_module("divya_bhaskar").db_probe_browsers()
        names = ["Automatic (any browser)"] + [
            f"{b}{'' if ok else '  (not found)'}" for b, ok in installed]
        combo = ttk.Combobox(row, textvariable=browser_var,
                             values=names, state="readonly", width=26)
        combo.pack(side="left", padx=(4, 0))
        combo.current(0)                 # Automatic is the default choice

        def _chosen_browser() -> str:
            text = browser_var.get()
            if text.startswith("Automatic"):
                return "Auto"
            return text.split("  ")[0].strip()

        def enable() -> None:
            browser = _chosen_browser()
            path = newspaper_module("divya_bhaskar").db_save_autologin(browser, "Default")
            label = ("Automatic (any browser)" if browser == "Auto"
                     else browser)
            status.configure(text=f"Currently ON - {label}")
            result.configure(
                text="Enabled.  Just press Extract - the login is read "
                     f"from {label} automatically.\nSaved: {path}")

        def test() -> None:
            browser = _chosen_browser()
            if browser == "Auto":
                cookie = newspaper_module("divya_bhaskar").db_import_browser_cookie(_SilentReporter())
            else:
                cookie = ""
                for prof in newspaper_module("divya_bhaskar")._chromium_profiles(os.path.expandvars(
                        dict(newspaper_module("divya_bhaskar").DB_BROWSER_ROOTS).get(browser, ""))) \
                        if dict(newspaper_module("divya_bhaskar").DB_BROWSER_ROOTS).get(browser) else \
                        ["Default"]:
                    cookie = newspaper_module("divya_bhaskar")._read_browser_cookie(browser, prof,
                                                  newspaper_module("divya_bhaskar").DB_COOKIE_DOMAINS)
                    if cookie and newspaper_module("divya_bhaskar")._db_cookie_is_authed(cookie):
                        break
            if cookie:
                keys = ", ".join(sorted(
                    p.split("=", 1)[0] for p in cookie.split("; ")))[:120]
                result.configure(
                    text=f"Success - read {len(cookie.split('; '))} "
                         f"cookies from {browser}: {keys}")
            else:
                result.configure(
                    text=f"Could not read a Divya Bhaskar cookie from "
                         f"{browser}.  Make sure you are logged in there; "
                         "if it is very new Chrome, try Edge or Open PDF.")

        def disable() -> None:
            newspaper_module("divya_bhaskar").db_clear_autologin()
            status.configure(text="Currently OFF")
            result.configure(text="Auto-login disabled.")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Test", width=10,
                   command=test).pack(side="left")
        ttk.Button(buttons, text="Enable", width=10,
                   command=enable).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Turn Off", width=10,
                   command=disable).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Close", width=10,
                   command=dialog.destroy).pack(side="right")
        result = ttk.Label(frame, text="", justify="left", wraplength=600,
                           foreground="#0a6b0a")
        result.pack(anchor="w", pady=(10, 0))
        dialog.bind("<Escape>", lambda _e: dialog.destroy())

    def _open_db_session_dialog(self) -> None:
        """Store the Divya Bhaskar premium session cookie once, so the app
        fetches pages as the logged-in user on every run."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Divya Bhaskar Login (Session Cookie)")
        dialog.geometry("680x460")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        db = newspaper_module("divya_bhaskar")
        ttk.Label(frame, justify="left", wraplength=640, text=(
            "Normally you do not need this dialog.\n\n"
            "Extract signs in for you: it opens the e-paper in a browser, "
            "which collects the session itself.  If no login exists on this "
            "machine, a sign-in window appears once and is remembered in "
            "notice_extractor/data/browser_profile.\n\n"
            "'Sign in now' does that step deliberately - use it to log in "
            "before a scheduled run, or to switch accounts.\n\n"
            "The box below is the manual fallback for a machine that cannot "
            "run the browser: paste a cookie header from your own browser "
            "and press Save.  It is stored in "
            + db.DB_SESSION_FILENAME + " under data/; clear the box and "
            "press Save to log out.")).pack(anchor="w")
        text = tk.Text(frame, height=5, wrap="word")
        text.pack(fill="both", expand=True, pady=8)
        text.insert("1.0", db.db_load_session_cookie(force=True))
        status = ttk.Label(frame, text="", wraplength=640, justify="left")
        status.pack(anchor="w")

        def save() -> None:
            cookie = text.get("1.0", "end").strip()
            try:
                path = db.db_save_session_cookie(cookie)
            except OSError as exc:
                status.configure(text=f"Could not save: {exc}")
                return
            status.configure(
                text=("Saved - will be used on every run:  " + path)
                if cookie else "Cleared - no session stored.")

        def sign_in() -> None:
            """Open the real sign-in window on a worker thread; Playwright
            blocks until the user is done, and Tk must keep running."""
            from .scrapers.browser_session import BrowserSession

            sign_in_btn.configure(state="disabled")
            status.configure(text="Opening a sign-in window...")
            day = config.resolve_target_date()
            url = (db._db_detail_page_url("ahmedabad", day.isoformat())
                   or "https://www.divyabhaskar.co.in/epaper")

            def work() -> None:
                session = BrowserSession(lambda m, level="info": None,
                                         headless=False)
                try:
                    ok = session.sign_in(url, db._db_cookie_is_logged_in)
                    cookie = session.cookie_header() if ok else ""
                finally:
                    session.close()
                if ok and cookie:
                    try:
                        db.db_save_session_cookie(cookie)
                    except OSError:
                        pass

                def finish() -> None:
                    sign_in_btn.configure(state="normal")
                    status.configure(
                        text="Signed in - the session is stored and every "
                             "run will use it."
                        if ok else
                        "Sign-in did not complete.  Try again, or paste a "
                        "cookie below.")
                    if ok:
                        text.delete("1.0", "end")
                        text.insert("1.0", db.db_load_session_cookie(True))

                self.root.after(0, finish)

            threading.Thread(target=work, daemon=True,
                             name="db-signin").start()

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        sign_in_btn = ttk.Button(buttons, text="Sign in now", width=14,
                                 command=sign_in)
        sign_in_btn.pack(side="left")
        ttk.Button(buttons, text="Save", width=10,
                   command=save).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Close", width=10,
                   command=dialog.destroy).pack(side="right")
        dialog.bind("<Escape>", lambda _e: dialog.destroy())

    def _open_proxy_dialog(self) -> None:
        """Set an HTTP proxy for office / LAN networks (blank = auto-detect
        the system proxy).  A quick reachability test is included."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Network (Proxy)")
        dialog.geometry("600x340")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, justify="left", wraplength=560, text=(
            "On an office / LAN network the internet often goes through a "
            "proxy.  Leave this blank to use your computer's own proxy "
            "settings automatically (recommended), or type the proxy here "
            "if the automatic one does not work.\n\n"
            "Format:  host:port   or   http://user:pass@host:port")
        ).pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(10, 4))
        ttk.Label(row, text="Proxy:").pack(side="left")
        proxy_var = tk.StringVar(value=load_proxy(force=True))
        entry = ttk.Entry(row, textvariable=proxy_var)
        entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        result = ttk.Label(frame, text="", justify="left", wraplength=560)
        result.pack(anchor="w", pady=(10, 0))

        def do_save() -> None:
            path = save_proxy(proxy_var.get().strip())
            result.configure(
                foreground="#0a6b0a",
                text=("Saved.  " + ("Using auto/system proxy."
                      if not proxy_var.get().strip()
                      else f"Proxy set to {proxy_var.get().strip()}")
                      + f"\n{path}"))

        def do_test() -> None:
            save_proxy(proxy_var.get().strip())
            result.configure(foreground="#333333",
                             text="Testing new-wapi.sandesh.com ...")
            dialog.update_idletasks()
            text, error = newspaper_module("sandesh")._sandesh_http(
                "https://new-wapi.sandesh.com/api/v1/menu/e-paper-menu", 15)
            if text is not None:
                result.configure(foreground="#0a6b0a",
                                 text="Success - the Sandesh API is "
                                      "reachable with these settings.")
            else:
                result.configure(foreground="#c00000",
                                 text=f"Still failing - {error}.")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Test", width=10,
                   command=do_test).pack(side="left")
        ttk.Button(buttons, text="Save", width=10,
                   command=do_save).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Close", width=10,
                   command=dialog.destroy).pack(side="right")
        dialog.bind("<Escape>", lambda _e: dialog.destroy())

    def _show_about(self) -> None:
        AboutDialog(self.root, self._guj_ui_font)

    # -- layout ---------------------------------------------------------------
    def _build_ui(self) -> None:
        # ---- Top: extraction controls --------------------------------------
        controls = ttk.LabelFrame(self, text="Extraction")
        controls.pack(fill="x", padx=8, pady=(8, 4))

        ttk.Label(controls, text="Newspaper:").grid(
            row=0, column=0, sticky="w", padx=(8, 4), pady=6)
        # Default to scanning every newspaper - that is what a normal run is.
        self.newspaper_var = tk.StringVar(value=ALL_NEWSPAPERS_LABEL)
        self.newspaper_combo = ttk.Combobox(
            controls, textvariable=self.newspaper_var, state="readonly",
            values=[ALL_NEWSPAPERS_LABEL] + list(NEWSPAPER_REGISTRY),
            width=22)
        self.newspaper_combo.grid(row=0, column=1, sticky="w", pady=6)

        ttk.Label(controls, text="E-paper URL:").grid(
            row=0, column=2, sticky="w", padx=(16, 4), pady=6)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(controls, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=3, sticky="ew", pady=6)
        self.url_entry.bind("<Return>", lambda _e: self.start_extraction())

        self.extract_btn = ttk.Button(controls, text="Extract",
                                      command=self.start_extraction, width=12)
        self.extract_btn.grid(row=0, column=4, padx=(10, 4), pady=6)
        self.cancel_btn = ttk.Button(controls, text="Cancel",
                                     command=self.cancel_extraction,
                                     width=10, state="disabled")
        self.cancel_btn.grid(row=0, column=5, padx=(0, 4), pady=6)
        self.open_pdf_btn = ttk.Button(controls, text="Open PDF...",
                                       command=self.open_pdf_file, width=11)
        self.open_pdf_btn.grid(row=0, column=6, padx=(0, 8), pady=6)

        # Row 1: date RANGE (From / To) + edition + dependency button.
        ttk.Label(controls, text="Dates:").grid(
            row=1, column=0, sticky="w", padx=(8, 4), pady=(0, 6))
        date_frame = ttk.Frame(controls)
        date_frame.grid(row=1, column=1, sticky="w", pady=(0, 6))
        # The date itself is the button: clicking either field opens the
        # calendar, so nobody has to discover a separate "..." control.
        ttk.Label(date_frame, text="From").pack(side="left")
        self.date_var = tk.StringVar()
        self.date_btn = ttk.Button(date_frame, textvariable=self.date_var,
                                   width=12, style="Date.TButton",
                                   command=self._open_date_picker)
        self.date_btn.pack(side="left", padx=(3, 0))
        ttk.Label(date_frame, text="To").pack(side="left", padx=(8, 0))
        self.date_to_var = tk.StringVar()
        self.date_to_btn = ttk.Button(date_frame,
                                      textvariable=self.date_to_var,
                                      width=12, style="Date.TButton",
                                      command=self._open_date_picker_to)
        self.date_to_btn.pack(side="left", padx=(3, 0))

        # Edition dropdown (shown only for papers that declare editions).
        self._edition_frame = ttk.Frame(controls)
        ttk.Label(self._edition_frame, text="Edition:").pack(side="left")
        self.edition_var = tk.StringVar()
        self.edition_combo = ttk.Combobox(self._edition_frame,
                                          textvariable=self.edition_var,
                                          width=15)
        self.edition_combo.pack(side="left", padx=(4, 0))
        self.edition_combo.bind("<<ComboboxSelected>>",
                                lambda _e: self._refresh_url())
        self.edition_combo.bind("<Return>", lambda _e: self._refresh_url())
        self._edition_frame.grid(row=1, column=2, sticky="w",
                                 padx=(16, 0), pady=(0, 6))

        # Archive-window note (Gujarat Samachar keeps only ~7 days online).
        limits = "  |  ".join(
            f"{cls.display_name}: "
            + (f"last {cls.days_back_limit} days"
               if cls.days_back_limit else "any date")
            for cls in NEWSPAPER_REGISTRY.values()
            if cls is not local_pdf_extractor())
        ttk.Label(controls, text="Archive: " + limits,
                  foreground="#7a4a00").grid(row=2, column=0, columnspan=7,
                                             sticky="w", padx=(8, 0),
                                             pady=(0, 6))

        # v1.14: only strict જાહેર નોટિસ are extracted - the old "broad"
        # notice-type checkbox was removed at the user's request.
        self.deps_btn = ttk.Button(controls, text="Download Dependencies",
                                   command=self.download_dependencies,
                                   width=22)
        self.deps_btn.grid(row=1, column=4, columnspan=2, sticky="e",
                           padx=(4, 8), pady=(0, 6))
        controls.columnconfigure(3, weight=1)

        # Selecting a newspaper, edition or date rebuilds the URL.
        self.newspaper_combo.bind("<<ComboboxSelected>>",
                                  lambda _e: self._on_newspaper_changed())

        # ---- Progress ------------------------------------------------------
        progress_frame = ttk.Frame(self)
        progress_frame.pack(fill="x", padx=8, pady=(0, 4))
        # Appears only while the log sidebar is collapsed.
        self._show_log_btn = ttk.Button(progress_frame, text="☰ Log",
                                        width=7, command=self.toggle_log)
        self.progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.phase_label = ttk.Label(progress_frame, text="Ready",
                                     width=28, anchor="e")
        self.phase_label.pack(side="right", padx=(8, 0))

        # ---- Middle: log | gallery | preview -------------------------------
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=4)
        self._paned = paned
        self._log_visible = True

        self.log_panel = StatusLogPanel(paned, on_close=self.toggle_log)
        paned.add(self.log_panel, weight=1)
        # The first layout pass decides the sash positions; claim a usable
        # width for the log before the gallery and preview take everything.
        self.after(120, self._restore_log_width)

        self.gallery = GalleryPanel(paned,
                                    on_open=self.open_preview_window,
                                    on_save=self.save_single,
                                    on_click=self.show_in_preview)
        self.gallery.on_search = self.start_search
        paned.add(self.gallery, weight=2)
        # Right of the dates, directly below the E-paper URL field (col 3 is
        # the URL's own - wide - column; deps button keeps cols 4-5).
        self.gallery.build_search_bar(controls).grid(
            row=1, column=3, sticky="ew", padx=(16, 0), pady=(0, 6))
        # Which notice type this run extracts.
        type_frame = ttk.Frame(controls)
        ttk.Label(type_frame, text="Notice type:").pack(side="left")
        self.notice_type_var = tk.StringVar(value=NOTICE_TYPE_CHOICES[0])
        type_combo = ttk.Combobox(type_frame,
                                  textvariable=self.notice_type_var,
                                  state="readonly", width=16,
                                  values=list(NOTICE_TYPE_CHOICES),
                                  font=(self._guj_ui_font, 10))
        type_combo.pack(side="left", padx=(4, 0))
        type_frame.grid(row=2, column=0, columnspan=3, sticky="w",
                        padx=(8, 0), pady=(0, 6))

        self.preview = ImagePreviewPanel(paned)
        paned.add(self.preview, weight=2)

        # ---- Bottom: save actions + status bar -----------------------------
        actions = ttk.Frame(self)
        actions.pack(fill="x", padx=8, pady=(0, 4))
        self.save_selected_btn = ttk.Button(
            actions, text="Save Selected...", command=self.save_selected,
            state="disabled", width=16)
        self.save_selected_btn.pack(side="right", padx=(6, 0))
        self.save_all_btn = ttk.Button(
            actions, text="Save All...", command=self.save_all,
            state="disabled", width=16)
        self.save_all_btn.pack(side="right")
        self.count_label = ttk.Label(actions, text="0 notices")
        self.count_label.pack(side="left")

        self.status_bar = ttk.Label(self, text="Ready", relief="sunken",
                                    anchor="w", padding=(6, 2))
        self.status_bar.pack(fill="x", side="bottom")

        # Pre-fill today's edition URL for the selected newspaper.  The log
        # stays empty until there is real progress or an error to show.
        self._update_edition_widgets()
        self._refresh_url()

    # -- date picker & URL auto-fill -----------------------------------------
    def _is_all_mode(self) -> bool:
        return self.newspaper_var.get() == ALL_NEWSPAPERS_LABEL

    def _current_extractor_cls(self) -> Type[BaseNewspaperExtractor]:
        return NEWSPAPER_REGISTRY.get(
            self.newspaper_var.get(),
            next(iter(NEWSPAPER_REGISTRY.values())))

    @staticmethod
    def _online_newspapers() -> List[Type[BaseNewspaperExtractor]]:
        """Every newspaper an 'All' run covers (local PDFs are excluded -
        they are opened explicitly with 'Open PDF...')."""
        return [cls for cls in NEWSPAPER_REGISTRY.values()
                if cls is not local_pdf_extractor()]

    def _on_newspaper_changed(self) -> None:
        self._update_edition_widgets()
        self._refresh_url()

    def _update_edition_widgets(self) -> None:
        if self._is_all_mode():
            self._edition_frame.grid_remove()
            return
        cls = self._current_extractor_cls()
        if cls.editions:
            self.edition_combo.configure(values=list(cls.editions))
            current = cls.edition_from_url(self.url_var.get().strip()) \
                or cls.default_edition
            self.edition_var.set(current)
            self._edition_frame.grid()
        else:
            self._edition_frame.grid_remove()

    def _picker_min_date(self) -> "date":
        """In All-Newspapers mode any date is selectable (each paper's own
        window is applied per job); otherwise the paper's own floor."""
        if self._is_all_mode():
            floors = [cls.min_date() for cls in self._online_newspapers()]
            return min(floors) if floors else date(2000, 1, 1)
        return self._current_extractor_cls().min_date()

    def _picker_note(self) -> str:
        if self._is_all_mode():
            short = [f"{cls.display_name}: last {cls.days_back_limit} days"
                     for cls in self._online_newspapers()
                     if cls.days_back_limit]
            return ("Older dates are fine - papers with a shorter archive "
                    "are skipped automatically.\n" + "  |  ".join(short)) \
                if short else ""
        cls = self._current_extractor_cls()
        if cls.days_back_limit:
            return (f"{cls.display_name} keeps only the last "
                    f"{cls.days_back_limit} days online - earlier dates are "
                    "greyed out.")
        return f"{cls.display_name}: any past date can be chosen."

    def _open_date_picker(self) -> None:
        DatePickerDialog(self.root, self._selected_date,
                         self._picker_min_date(), date.today(),
                         self._on_date_picked, note=self._picker_note())

    def _on_date_picked(self, day: "date") -> None:
        self._selected_date = day
        if self._selected_date_to < day:
            self._selected_date_to = day
        self._refresh_url()

    def _open_date_picker_to(self) -> None:
        DatePickerDialog(self.root, self._selected_date_to,
                         self._picker_min_date(), date.today(),
                         self._on_date_to_picked, note=self._picker_note())

    def _on_date_to_picked(self, day: "date") -> None:
        self._selected_date_to = day
        if day < self._selected_date:
            self._selected_date = day
        self._refresh_url()

    def _date_range(self) -> List["date"]:
        """Every date From..To (inclusive), oldest first, capped."""
        start = min(self._selected_date, self._selected_date_to)
        end = max(self._selected_date, self._selected_date_to)
        days: List["date"] = []
        current = start
        while current <= end and len(days) < MAX_RANGE_DAYS:
            days.append(current)
            current += timedelta(days=1)
        return days

    def _refresh_url(self) -> None:
        """Rebuild the URL from the selected newspaper + date.  Clamps the
        date to the paper's archive window (e.g. Gujarat Samachar: last
        7 days) and keeps the edition from the current URL when possible."""
        self.date_var.set(self._selected_date.strftime("%d-%m-%Y"))
        self.date_to_var.set(self._selected_date_to.strftime("%d-%m-%Y"))
        if self._is_all_mode():
            self._edition_frame.grid_remove()
            self.url_var.set("")
            days = len(self._date_range())
            self.status_bar.configure(
                text=f"All Newspapers: every paper's monitored editions "
                     f"for {days} day(s).  Press Extract.")
            return
        cls = self._current_extractor_cls()
        today = date.today()
        if self._selected_date > today:
            self._selected_date = today
        minimum = cls.min_date()
        if self._selected_date < minimum:
            self._selected_date = today
            self._selected_date_to = max(self._selected_date_to, today)
            self.status_bar.configure(
                text=f"{cls.display_name} only covers the last "
                     f"{cls.days_back_limit} days - date reset to today.")
        self.date_var.set(self._selected_date.strftime("%d-%m-%Y"))
        self.date_to_var.set(self._selected_date_to.strftime("%d-%m-%Y"))
        if cls.editions:
            edition = (self.edition_var.get().strip().lower()
                       .replace(" ", "-") or cls.default_edition)
        else:
            edition = cls.edition_from_url(self.url_var.get().strip()) \
                or cls.default_edition
        built = cls.build_url(edition, self._selected_date)
        if built:
            self.url_var.set(built)
        else:
            self.url_var.set("")
            self.status_bar.configure(
                text="Use 'Open PDF...' to choose a newspaper PDF file.")

    # -- extraction control ---------------------------------------------------
    def start_extraction(self) -> None:
        if self._running:
            return
        # All-Newspapers / multi-day runs go through the job driver.
        if self._is_all_mode() or len(self._date_range()) > 1:
            self._start_job_run()
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning(APP_NAME, "Please enter an e-paper URL.",
                                   parent=self.root)
            return

        selected_name = self.newspaper_var.get()
        selected_cls = NEWSPAPER_REGISTRY.get(selected_name)
        detected_cls = find_extractor_for_url(url)

        if detected_cls is None:
            messagebox.showerror(
                APP_NAME,
                "This URL is not recognized by any supported newspaper "
                "extractor.\n\nSupported:\n"
                "  Gujarat Samachar:  https://epaper.gujaratsamachar.com/"
                "<edition>/DD-MM-YYYY/<page>\n"
                "  Sandesh:  https://sandesh.com/epaper/"
                "<edition>?date=YYYY-MM-DD&page=1\n"
                "  Divya Bhaskar:  https://www.divyabhaskar.co.in/epaper"
                "?edition=<edition>&date=YYYY-MM-DD\n"
                "  (or paste any e-paper URL straight from your browser)\n"
                "  Nav Gujarat Samay:  https://epaper.navgujaratsamay.com"
                "/reader/<issueId>/Ahmedabad/08-AUG-2026/page/1/1\n"
                "  PDF file:  press 'Open PDF...', or paste a .pdf path "
                "or a direct .pdf web link",
                parent=self.root)
            return
        if selected_cls is not detected_cls:
            # The URL wins; keep the dropdown honest.
            self.newspaper_var.set(detected_cls.display_name)
            self.log_panel.log(
                f"URL belongs to {detected_cls.display_name}; switched "
                "newspaper selection.", "warn")

        self._begin_run()
        reporter = ProgressReporter(self._msg_queue, self._cancel_event)
        extractor = detected_cls(broad=False)
        extractor.current_issue_date = self._selected_date.isoformat()
        heading = (f"{detected_cls.display_name}  -  "
                   f"{self._selected_date.strftime('%d-%m-%Y')}")

        def run():
            reporter.heading(heading)
            extractor.extract(url, reporter)

        self._worker = threading.Thread(target=run, daemon=True,
                                        name="extraction-worker")
        self._worker.start()

    def _begin_run(self) -> None:
        """Shared UI state reset when any extraction run starts.

        Fresh queue + cancel event per run: a cancelled worker keeps its
        own (now-orphaned) queue, so its late messages never reach the UI
        and Cancel can return control instantly."""
        self._running = True
        # The notice-type toggle applies per run; the agents and their
        # template verifiers all read it at construction time.
        set_notice_type(self.notice_type_var.get())
        self._msg_queue = queue.Queue()
        self._cancel_event = threading.Event()
        self.extract_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.save_all_btn.configure(state="disabled")
        self.save_selected_btn.configure(state="disabled")
        self.gallery.clear()
        self.preview.clear()
        self.log_panel.clear()
        self._paper_counts = {}
        self._result_seq = 0
        self.progress.configure(value=0, maximum=1)
        self._set_phase("Starting...")
        self._update_count()

    def open_pdf_file(self) -> None:
        """Pick a local e-paper PDF and extract notices from every page."""
        if self._running:
            return
        pdf_cls = local_pdf_extractor()
        if pdf_cls is None:
            messagebox.showerror(
                APP_NAME, "The local-PDF plugin (scrapers/local_pdf.py) "
                "is not loaded.", parent=self.root)
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="Open e-paper PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if not path:
            return
        self.newspaper_var.set(pdf_cls.display_name)
        self._update_edition_widgets()
        self.url_var.set(path)
        self.start_extraction()

    # -- multi-newspaper / multi-day driver -----------------------------------
    def _build_jobs(self) -> List[Tuple[Type[BaseNewspaperExtractor],
                                        str, "date", str]]:
        """(extractor, edition, date, url) for every combination to run."""
        jobs: List[Tuple[Type[BaseNewspaperExtractor], str,
                         "date", str]] = []
        papers = (self._online_newspapers() if self._is_all_mode()
                  else [self._current_extractor_cls()])
        for day in self._date_range():
            for cls in papers:
                if day < cls.min_date():
                    continue          # outside this paper's archive window
                if self._is_all_mode():
                    editions = list(cls.get_loop_editions())
                elif cls.editions:
                    editions = [(self.edition_var.get().strip()
                                 or cls.default_edition)]
                else:
                    editions = [cls.default_edition]
                for edition in editions:
                    url = cls.build_url(edition, day)
                    jobs.append((cls, edition, day, url))
        return jobs

    def _start_job_run(self) -> None:
        """Run every (newspaper, edition, date) job into one gallery, with a
        heading per job and per-newspaper totals at the end.

        The pipeline itself lives in agents/processor.py so the same run can
        happen without a window (main.py --headless)."""
        from .agents.processor import run_jobs      # avoids an import cycle

        jobs = self._build_jobs()
        if not jobs:
            messagebox.showinfo(
                APP_NAME, "Nothing to extract for the chosen dates.",
                parent=self.root)
            return
        self._begin_run()
        days = len(self._date_range())
        self.log_panel.log(
            f"Running {len(jobs)} edition(s) across {days} day(s).", "info")
        run_logger.banner(f"{APP_TITLE}: {len(jobs)} edition(s), "
                          f"{days} day(s)")

        reporter = ProgressReporter(self._msg_queue, self._cancel_event)
        self._worker = threading.Thread(
            target=run_jobs, args=(jobs, reporter), daemon=True,
            name="job-run-worker")
        self._worker.start()

    def cancel_extraction(self) -> None:
        if self._running:
            self._cancel_event.set()
            # Return control to the user immediately - the worker thread
            # unwinds in the background and its messages go to an orphaned
            # queue, so nothing stale reaches the UI.
            self.log_panel.log("Extraction cancelled by user.", "warn")
            self._extraction_finished("Extraction cancelled.")

    def _extraction_finished(self, status_text: str) -> None:
        self._running = False
        self.gallery.on_page_change = self._update_count
        self.gallery.follow_live = False      # stay where the user is
        self.gallery.goto_page(0, user=False)  # start at the first paper
        self.extract_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        has_results = bool(self.gallery.all_results())
        state = "normal" if has_results else "disabled"
        self.save_all_btn.configure(state=state)
        self.save_selected_btn.configure(state=state)
        self._set_phase("Done")
        self.status_bar.configure(text=status_text)

    # -- dependency installer -------------------------------------------------
    def download_dependencies(self) -> None:
        """Install/upgrade every dependency with pip, streaming pip's output
        into the status log."""
        if self._installing:
            return
        if self._running:
            messagebox.showinfo(
                APP_NAME, "Please wait for the current extraction to finish "
                "(or press Cancel) before installing dependencies.",
                parent=self.root)
            return
        if not messagebox.askyesno(
                APP_NAME,
                "Download and set up everything now?\n\n"
                "1. Python packages:\n    "
                + ", ".join(all_pip_packages())
                + "\n\n2. Tesseract OCR program (if missing)\n    installs "
                  "into " + preferred_tesseract_dir()
                + "\n\n3. Gujarati language data (guj.traineddata)\n    into "
                + local_tessdata_dir()
                + "\n\nEasyOCR is skipped on purpose: ~2 GB of torch for a "
                  "backend that has no Gujarati model.\n\n"
                  "An internet connection is required; this can take "
                  "several minutes.", parent=self.root):
            return
        self._installing = True
        self.deps_btn.configure(state="disabled")
        self.extract_btn.configure(state="disabled")
        self._set_phase("Installing dependencies...")
        self.log_panel.log("Downloading dependencies...", "info")

        msg_queue = self._msg_queue
        threading.Thread(
            target=pip_install_dependencies,
            args=(lambda line: msg_queue.put(("log", "  " + line, "dim")),
                  lambda rc: msg_queue.put(("deps_done", rc))),
            daemon=True, name="deps-installer").start()

    def _dependencies_finished(self, returncode: int) -> None:
        self._installing = False
        self.deps_btn.configure(state="normal")
        if not self._running:
            self.extract_btn.configure(state="normal")
        if returncode == 0:
            self._set_phase("Dependencies installed")
            self.log_panel.log("All dependencies installed successfully.",
                               "success")
            # A new winsdk/pytesseract may have just landed - re-probe the
            # backend chain instead of reusing this session's verdict.
            reset_ocr_engine_cache()
            if messagebox.askyesno(
                    APP_NAME, "Dependencies were installed/updated.\n\n"
                    "Restart the application now to apply them?",
                    parent=self.root):
                restart_application()
        else:
            self._set_phase("Dependency install failed")
            command = (sys.executable + " -m pip install --upgrade " +
                       " ".join(list(DEPENDENCY_PACKAGES) +
                                list(OPTIONAL_PACKAGES)))
            self.log_panel.log(
                "Dependency install failed.  Close this application and run "
                "this command manually:\n  " + command, "error")

    # -- application-wide mouse-wheel routing ---------------------------------
    # Tk delivers wheel events to the widget under the pointer WITHOUT
    # bubbling to parents, so a wheel spun over a thumbnail image would not
    # scroll the gallery.  A single global handler finds the widget under
    # the pointer, walks up its ancestry and scrolls the owning panel.
    def _install_global_wheel(self) -> None:
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(sequence, self._on_global_wheel, add="+")

    def _on_global_wheel(self, event):
        try:
            widget = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            widget = None
        if widget is None:
            widget = event.widget if isinstance(event.widget, tk.Misc) \
                else None
        while widget is not None:
            if isinstance(widget, ImagePreviewPanel):
                if event.state & 0x0004:          # Ctrl held -> zoom
                    widget._on_ctrl_wheel(event)
                else:
                    widget._on_mousewheel(event)
                return "break"
            if isinstance(widget, GalleryPanel):
                widget._on_mousewheel(event)
                return "break"
            widget = getattr(widget, "master", None)
        return None

    # -- queue polling (worker -> GUI) ---------------------------------------
    def _poll_queue(self) -> None:
        try:
            while True:
                message = self._msg_queue.get_nowait()
                self._handle_message(message)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _handle_message(self, message: tuple) -> None:
        kind = message[0]
        if kind == "log":
            self.log_panel.log(message[1], message[2])
            # Same line to data/logs/run-<date>.log: the on-screen log is
            # trimmed while a run is going and gone when the app closes.
            run_logger.log(message[1], message[2])
        elif kind == "phase":
            self._set_phase(message[1])
        elif kind == "progress":
            current, total = message[1], message[2]
            self.progress.configure(maximum=max(1, total), value=current)
        elif kind == "heading":
            self.gallery.add_heading(message[1])
            self.gallery.on_page_change = self._update_count
        elif kind == "result":
            result: NoticeResult = message[1]
            # Numbered here, on the Tk thread, because notices now stream in
            # from every edition agent at once - the worker cannot number them.
            self._result_seq += 1
            result.result_id = self._result_seq
            self._paper_counts[result.newspaper or "?"] = \
                self._paper_counts.get(result.newspaper or "?", 0) + 1
            self.gallery.add_result(result)
            self._update_count()
            if len(self.gallery.cards) == 1:
                self.show_in_preview(result)
        elif kind == "done":
            self._extraction_finished(message[1])
        elif kind == "failed":
            self._extraction_finished("Extraction failed.")
            messagebox.showerror(APP_NAME, message[1], parent=self.root)
        elif kind == "cancelled":
            self._extraction_finished("Extraction cancelled.")
        elif kind == "search_done":
            self._searching = False
            _query, matched, scanned = message[1], message[2], message[3]
            self.gallery.search_finished(_query, matched, scanned)
            self._update_count()
        elif kind == "deps_done":
            self._dependencies_finished(message[1])

    # -- log sidebar ----------------------------------------------------------
    def toggle_log(self) -> None:
        """Collapse / restore the Status Log sidebar.  Logging keeps running
        while it is hidden - the Text widget buffers off-screen, so nothing
        is missed when it comes back."""
        if self._log_visible:
            self._paned.forget(self.log_panel)
            self._show_log_btn.pack(side="left", padx=(0, 8),
                                    before=self.progress)
        else:
            self._paned.insert(0, self.log_panel, weight=1)
            self._show_log_btn.pack_forget()
            # A re-inserted ttk pane comes back at zero width: the panel is
            # "open" but invisible, which is exactly the bug being fixed.
            self.after_idle(self._restore_log_width)
        self._log_visible = not self._log_visible

    def _restore_log_width(self, tries: int = 12) -> None:
        """Give the log pane a real width (and never less than the minimum).

        Sash positions only stick once the paned window has been laid out, so
        this waits for a real width before setting one - on a slow first paint
        an early call is silently ignored, which is how the pane ended up at
        zero in the first place."""
        if not self._log_visible:
            return
        try:
            if self._paned.winfo_width() <= 1:      # not laid out yet
                if tries > 0:
                    self.after(100, self._restore_log_width, tries - 1)
                return
            if self._paned.sashpos(0) < LOG_PANE_MIN_WIDTH:
                self._paned.sashpos(0, LOG_PANE_WIDTH)
        except (tk.TclError, IndexError):
            pass

    # -- text search across the cropped notices -------------------------------
    def start_search(self, query: str) -> None:
        """Find `query` inside the detected notices and highlight the words.

        The crops have never been read end to end - detection only OCRs
        header strips - so the first search reads each one and caches the
        words on the result.  That happens on a worker thread; the UI stays
        responsive and the answer arrives as a 'search_done' message."""
        results = self.gallery.all_results()
        if not results:
            self.gallery.search_finished(query, 0, 0)
            return
        if self._searching:
            return
        self._searching = True
        msg_queue = self._msg_queue

        def run() -> None:
            matched = 0
            try:
                engine = select_ocr_engine(_SilentReporter())
                pending = [r for r in results if not r.ocr_done]
                if engine is not None and pending:
                    msg_queue.put(("log", f"[Search] reading {len(pending)} "
                                          "notice(s) with "
                                          f"{engine.name}...", "dim"))

                    def read(result: NoticeResult) -> None:
                        try:
                            gray = cv2.cvtColor(result.image_bgr,
                                                cv2.COLOR_BGR2GRAY)
                            result.ocr_words = engine.read_words(gray)
                            result.ocr_text = " ".join(
                                w.text for w in result.ocr_words)
                        except Exception:
                            result.ocr_words = []
                            result.ocr_text = ""
                        finally:
                            result.ocr_done = True

                    list(get_ocr_pool().map(read, pending))

                # Token search (utils/search.py): every word of the query has
                # to appear somewhere in the notice, in any order.  A notice
                # can match without highlightable boxes - OCR sometimes glues
                # the phrase together - so the count comes from the flag.
                for result in results:
                    hit, boxes = search_notice(result.ocr_words,
                                               result.ocr_text, query)
                    result.match_boxes = boxes
                    if hit:
                        matched += 1
            except Exception:
                msg_queue.put(("log", "[Search] failed:\n"
                               + traceback.format_exc(), "error"))
            finally:
                msg_queue.put(("search_done", query, matched, len(results)))

        threading.Thread(target=run, daemon=True, name="notice-search").start()

    def _set_phase(self, text: str) -> None:
        self.phase_label.configure(text=text)
        self.status_bar.configure(text=text)

    def _update_count(self) -> None:
        count = len(self.gallery.all_results())
        text = f"{count} notice{'s' if count != 1 else ''}"
        label = self.gallery.page_label()
        if label:
            text += f"   -   {label}"
        if len(self._paper_counts) > 1:
            parts = ", ".join(f"{name}: {n}" for name, n
                              in sorted(self._paper_counts.items()))
            text += f"   ({parts})"
        self.count_label.configure(text=text)

    # -- preview & saving -----------------------------------------------------
    def show_in_preview(self, result: NoticeResult) -> None:
        self.preview.show_result(result)

    def open_preview_window(self, result: NoticeResult) -> None:
        PreviewWindow(self.root, result)

    def save_single(self, result: NoticeResult) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save Notice Image",
            defaultextension=".png",
            initialfile=result.suggested_filename,
            filetypes=[("PNG image", "*.png"), ("JPEG image", "*.jpg"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            save_image_unicode(result.image_bgr, path)
            self.status_bar.configure(text=f"Saved {os.path.basename(path)}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not save image:\n{exc}",
                                 parent=self.root)

    def save_selected(self) -> None:
        self._save_many(self.gallery.selected_results(), "selected")

    def save_all(self) -> None:
        self._save_many(self.gallery.all_results(), "all")

    def _save_many(self, results: List[NoticeResult], label: str) -> None:
        if not results:
            messagebox.showinfo(
                APP_NAME, f"There are no {label} notices to save.",
                parent=self.root)
            return
        directory = filedialog.askdirectory(
            parent=self.root, title=f"Choose a folder to save {label} notices")
        if not directory:
            return

        existing = [r for r in results if os.path.exists(
            os.path.join(directory, r.suggested_filename))]
        if existing and not messagebox.askyesno(
                APP_NAME,
                f"{len(existing)} file(s) already exist in that folder.\n"
                "Overwrite them?", parent=self.root):
            return

        saved, errors = 0, 0
        for result in results:
            path = os.path.join(directory, result.suggested_filename)
            try:
                save_image_unicode(result.image_bgr, path)
                saved += 1
            except Exception as exc:
                errors += 1
                self.log_panel.log(f"Save failed for "
                                   f"{result.suggested_filename}: {exc}",
                                   "error")
        text = f"Saved {saved} notice{'s' if saved != 1 else ''} to " \
               f"{directory}"
        if errors:
            text += f"  ({errors} failed - see log)"
        self.status_bar.configure(text=text)
        self.log_panel.log(text, "success" if not errors else "warn")

    # -- shutdown -------------------------------------------------------------
    def _on_close(self) -> None:
        if self._running:
            if not messagebox.askyesno(
                    APP_NAME, "Extraction is still running.  Quit anyway?",
                    parent=self.root):
                return
            self._cancel_event.set()
        shutdown_ocr_pool()
        self.root.destroy()


# =============================================================================
# 10. MAIN
# =============================================================================

def _enable_windows_dpi_awareness() -> None:
    """Crisp rendering on high-DPI Windows displays."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _run_dependency_bootstrap() -> int:
    """Small setup window shown when required packages are missing: one
    click downloads everything with pip, then restarts the application."""
    root = tk.Tk()
    root.title(f"{APP_NAME} - Setup")
    root.geometry("620x420")
    root.minsize(520, 340)

    frame = ttk.Frame(root, padding=12)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Some required packages are missing:",
              font=("Segoe UI", 10, "bold")).pack(anchor="w")
    ttk.Label(frame, text="    " + ", ".join(_MISSING_DEPENDENCIES)
              ).pack(anchor="w", pady=(2, 8))
    ttk.Label(frame, text="Click the button below to download and install "
                          "them automatically (internet required).",
              wraplength=560).pack(anchor="w")

    output = tk.Text(frame, height=12, state="disabled", relief="sunken",
                     borderwidth=1, font=("Consolas", 9))
    output.pack(fill="both", expand=True, pady=8)

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x")
    msg_queue: "queue.Queue" = queue.Queue()

    def log_line(text: str) -> None:
        output.configure(state="normal")
        output.insert("end", text + "\n")
        output.see("end")
        output.configure(state="disabled")

    def start_install() -> None:
        install_btn.configure(state="disabled")
        threading.Thread(
            target=pip_install_dependencies,
            args=(lambda line: msg_queue.put(("line", line)),
                  lambda rc: msg_queue.put(("done", rc))),
            daemon=True).start()

    def poll() -> None:
        try:
            while True:
                kind, value = msg_queue.get_nowait()
                if kind == "line":
                    log_line(value)
                elif kind == "done":
                    if value == 0:
                        log_line("Done.  Restarting...")
                        root.after(800, restart_application)
                    else:
                        log_line("Install failed - see messages above.")
                        install_btn.configure(state="normal")
        except queue.Empty:
            pass
        root.after(100, poll)

    install_btn = ttk.Button(buttons, text="Download Dependencies",
                             command=start_install, width=24)
    install_btn.pack(side="left")
    ttk.Button(buttons, text="Exit", command=root.destroy,
               width=10).pack(side="right")
    poll()
    root.mainloop()
    return 1


def missing_dependencies() -> List[str]:
    """Core pip packages that failed to import."""
    return list(_MISSING_DEPENDENCIES)


def run_dependency_bootstrap() -> int:
    """The install-first window shown when core packages are missing."""
    return _run_dependency_bootstrap()


def main(newspaper_package=None) -> int:
    """Start the GUI.

    `newspaper_package` is the 'newspapers' plugin package; its modules are
    imported on a background thread by the launcher, so this only waits for
    that to finish and publishes the result to the registry."""
    _enable_windows_dpi_awareness()

    if _MISSING_DEPENDENCIES:
        return _run_dependency_bootstrap()

    if newspaper_package is not None:
        register_newspapers(newspaper_package.load_all())
    if not NEWSPAPER_REGISTRY:
        message = ("No newspaper modules could be loaded from "
                   "notice_extractor/scrapers - the application cannot run.")
        if newspaper_package is not None:
            for name, problem in newspaper_package.errors():
                message += f"\n\n{name}:\n{problem.strip()[-400:]}"
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, message)
        root.destroy()
        return 2

    root = tk.Tk()
    Application(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    # Normally started via main.py / the launcher, but running the core
    # directly should still work.
    from . import scrapers as _scrapers
    _scrapers.start_background_load()
    sys.exit(main(_scrapers))
