"""Central configuration - paths, the run date, browser and log settings.

Everything the app writes lives under `notice_extractor/data/`; everything it
reads that ships with the program (tessdata/) lives next to the project root.
Both are derived from this file's own location, so the app behaves the same
whether it is started from the launcher, from `python -m`, or from a shortcut
in another folder.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import date
from typing import List, Optional

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)
DATA_DIR = os.path.join(PACKAGE_DIR, "data")


def machine_cache_dir() -> str:
    """Scratch space OUTSIDE the project, for things that are neither source
    nor data: the browser profile and the interpreter's .pyc caches.

    Chromium keeps ~300 files of profile state (History, Cache, Preferences,
    Web Data, ...) and Python writes a __pycache__ next to every module.
    Both are machine-local junk, and a source tree is the wrong place for
    them - they drown the real files in every editor and search."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "PublicNoticeExtractor")
    return os.path.join(os.path.expanduser("~"), ".cache",
                        "public-notice-extractor")


CACHE_DIR = machine_cache_dir()
PYCACHE_DIR = os.path.join(CACHE_DIR, "pycache")


def data_path(*parts: str) -> str:
    """Path inside data/, creating the folder on the way."""
    path = os.path.join(DATA_DIR, *parts)
    os.makedirs(os.path.dirname(path) if os.path.splitext(path)[1]
                else path, exist_ok=True)
    return path


# --- run date -----------------------------------------------------------------
# "auto" = today.  Set an ISO date ("2026-08-08") to pin every run to one day,
# or export PNE_TARGET_DATE in the environment for a single session.
TARGET_DATE: str = os.environ.get("PNE_TARGET_DATE", "auto")


def resolve_target_date(override: str = "") -> date:
    """The date a run should extract.  Order: explicit override, TARGET_DATE,
    today.  An unparsable value falls back to today rather than crashing a
    scheduled run."""
    for candidate in (override, TARGET_DATE):
        text = (candidate or "").strip().lower()
        if not text or text in ("auto", "today"):
            continue
        try:
            return date.fromisoformat(text)
        except ValueError:
            continue
    return date.today()


# --- headless browser (Divya Bhaskar session handling) ------------------------
# The e-paper viewer is a client-rendered app behind a login.  A real browser
# gets the session cookies itself, so nothing has to be copied out of DevTools.
BROWSER_ENABLED = True
BROWSER_HEADLESS = True
#: "" = Playwright's own Chromium.  "chrome" / "msedge" use the browser already
#: installed on this machine (same engine, user's own fonts and codecs).
BROWSER_CHANNEL = ""
#: Persistent profile: the login survives between runs, so the one-time
#: sign-in really is one time.  Kept in the machine cache, not the project -
#: it is a whole Chromium profile, not a file anyone wants to read.
BROWSER_PROFILE_DIR = os.path.join(CACHE_DIR, "browser_profile")
BROWSER_NAV_TIMEOUT_MS = 45_000
#: How long to wait for the viewer's own XHRs to go quiet after load.
BROWSER_SETTLE_MS = 6_000
#: A run that finds no session may open a visible window ONCE so the user can
#: sign in; after that the stored profile is reused headlessly forever.
BROWSER_ALLOW_INTERACTIVE_LOGIN = True
BROWSER_LOGIN_WAIT_SECONDS = 240

# --- transient run data -------------------------------------------------------
# A run leaves nothing behind.  The cropped notices live in memory and reach
# the disk only when you press Save (or pass --save), to the folder you pick;
# the diagnostics the app writes for itself are wiped when it closes.
CLEAR_DATA_ON_EXIT = True
#: Folders under data/ that hold run output only, and are removed on exit.
#: A deny-list, not "delete everything else": whatever you deliberately put
#: in data/ is yours, and the app must never take it away.
TRANSIENT_DIRS: tuple = ("debug", "logs", "cache")
#: Kept on purpose - this is the Divya Bhaskar login and the app's settings.
#: Clearing it would mean signing in again on every launch, which is the one
#: manual step the automation exists to remove.
PERSISTENT_NAMES: tuple = ("browser_profile", "divyabhaskar_session.txt",
                           "divyabhaskar_autologin.json",
                           "network_proxy.txt",
                           # Recent searches: a history that empties itself
                           # every time the app closes is not a history.
                           "recent_searches.json",
                           # What the user told us, and what was learned from
                           # it.  Losing this on exit would mean the app
                           # forgets every correction the moment it closes.
                           "feedback.jsonl",
                           "learned.json")


def clear_run_data() -> list:
    """Delete this run's leftovers.  Returns the folders that went.

    A folder can survive: Windows will not delete a file another process has
    open, so a headless run going on in parallel keeps its own log. That is
    why this reports what it managed rather than asserting success."""
    from .utils import logger        # late: logger imports config
    logger.close()                   # our own handle would block logs/

    removed = []
    for name in TRANSIENT_DIRS:
        path = os.path.join(DATA_DIR, name)
        if not os.path.isdir(path):
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            removed.append(name)
    return removed


def use_external_pycache() -> None:
    """Send .pyc files to the machine cache instead of the source tree.

    Must run BEFORE the package is imported - bytecode caching is decided at
    import time - which is why the launcher does it inline rather than
    calling this."""
    sys.pycache_prefix = PYCACHE_DIR


def clear_pycache() -> List[str]:
    """Remove any __pycache__ folders that landed in the project anyway
    (a test run, a tool, or someone importing the package by hand)."""
    removed: List[str] = []
    for root, dirs, _files in os.walk(PROJECT_ROOT):
        if "__pycache__" not in dirs:
            continue
        path = os.path.join(root, "__pycache__")
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            removed.append(os.path.relpath(path, PROJECT_ROOT))
        dirs.remove("__pycache__")
    return removed


def migrate_browser_profile() -> bool:
    """Move a profile left in data/ by an earlier version out to the cache.

    Moved, not recreated: the profile IS the Divya Bhaskar login, and
    starting a fresh one would ask the user to sign in again."""
    legacy = os.path.join(DATA_DIR, "browser_profile")
    if not os.path.isdir(legacy) or os.path.isdir(BROWSER_PROFILE_DIR):
        return False
    try:
        os.makedirs(os.path.dirname(BROWSER_PROFILE_DIR), exist_ok=True)
        shutil.move(legacy, BROWSER_PROFILE_DIR)
        return True
    except OSError:
        return False


def _is_inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.abspath(path), folder]) == folder
    except ValueError:                    # different drives - so, no
        return False


def is_inside_data(path: str) -> bool:
    """Is `path` inside data/ (where the app deletes its own leftovers)?"""
    return _is_inside(path, DATA_DIR)


def is_inside_project(path: str) -> bool:
    """Is `path` inside the source tree?  Machine junk must not be."""
    return _is_inside(path, PROJECT_ROOT)


# --- logging ------------------------------------------------------------------
LOG_DIR = os.path.join(DATA_DIR, "logs")
LOG_TO_FILE = True
LOG_MAX_LINES = 4000          # on-screen Status Log trim point
LOG_KEEP_DAYS = 14            # older run logs are deleted on startup

# --- search -------------------------------------------------------------------
#: Similarity a Gujarati OCR word needs to count as a hit (1.0 = exact only).
SEARCH_FUZZY_RATIO = 0.80
#: Tokens shorter than this must match exactly - fuzzy matching two-letter
#: words matches everything.
SEARCH_FUZZY_MIN_LEN = 4


def session_file(name: str) -> str:
    """A saved-session file inside data/, migrating a legacy copy that still
    sits in the project root (older versions saved them there)."""
    target = os.path.join(DATA_DIR, name)
    legacy = os.path.join(PROJECT_ROOT, name)
    if not os.path.exists(target) and os.path.exists(legacy):
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            os.replace(legacy, target)
        except OSError:
            return legacy
    os.makedirs(DATA_DIR, exist_ok=True)
    return target


def debug_dir(name: str) -> str:
    """Folder for diagnostic dumps (one per newspaper)."""
    path = os.path.join(DATA_DIR, "debug", name)
    os.makedirs(path, exist_ok=True)
    return path


def tessdata_dir() -> Optional[str]:
    """The Gujarati model folder shipped with the project (root/tessdata)."""
    path = os.path.join(PROJECT_ROOT, "tessdata")
    return path if os.path.isdir(path) else None
