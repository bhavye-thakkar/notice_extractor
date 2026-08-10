"""Central configuration - paths, the run date, browser and log settings.

Everything the app writes lives under `notice_extractor/data/`; everything it
reads that ships with the program (tessdata/) lives next to the project root.
Both are derived from this file's own location, so the app behaves the same
whether it is started from the launcher, from `python -m`, or from a shortcut
in another folder.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Optional

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)
DATA_DIR = os.path.join(PACKAGE_DIR, "data")


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
#: Persistent profile: the login survives between runs, so the one-time sign-in
#: really is one time.
BROWSER_PROFILE_DIR = os.path.join(DATA_DIR, "browser_profile")
BROWSER_NAV_TIMEOUT_MS = 45_000
#: How long to wait for the viewer's own XHRs to go quiet after load.
BROWSER_SETTLE_MS = 6_000
#: A run that finds no session may open a visible window ONCE so the user can
#: sign in; after that the stored profile is reused headlessly forever.
BROWSER_ALLOW_INTERACTIVE_LOGIN = True
BROWSER_LOGIN_WAIT_SECONDS = 240

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
