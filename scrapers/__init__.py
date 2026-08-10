"""Newspaper scrapers - one module per paper.

Adding a newspaper means adding ONE file to this folder.  A module qualifies
by exposing a module-level `NEWSPAPER` naming its extractor class; nothing
else in the app has to change.

Modules are imported on a background thread (see start_background_load) so
the main window can appear while the heavier ones are still loading.

browser_session.py is shared plumbing, not a newspaper: it has no NEWSPAPER
attribute, so the loader skips it (see PLUMBING below).
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import threading
import traceback
from typing import Callable, Dict, List, Optional, Tuple

#: Order the GUI lists them in.  Papers found on disk but not named here are
#: appended afterwards, alphabetically, so a dropped-in file still works.
PREFERRED_ORDER: Tuple[str, ...] = (
    "gujarat_samachar",
    "sandesh",
    "divya_bhaskar",
    "nav_gujarat_samay",
    "local_pdf",
)

#: Shared helpers that live here but are not newspapers.
PLUMBING: Tuple[str, ...] = ("browser_session",)

_lock = threading.Lock()
_loaded: Dict[str, type] = {}
_errors: List[Tuple[str, str]] = []
_thread: Optional[threading.Thread] = None
_done = threading.Event()


def module_names() -> List[str]:
    """Every plugin module in this folder, in display order."""
    found = sorted(
        name for _finder, name, is_pkg in pkgutil.iter_modules([
            os.path.dirname(os.path.abspath(__file__))])
        if not is_pkg and not name.startswith("_")
        and name not in PLUMBING)
    ordered = [n for n in PREFERRED_ORDER if n in found]
    ordered += [n for n in found if n not in PREFERRED_ORDER]
    return ordered


def _load_now() -> None:
    """Import every plugin.  One broken module must not hide the others."""
    for name in module_names():
        try:
            module = importlib.import_module(f"{__name__}.{name}")
            extractor = getattr(module, "NEWSPAPER", None)
            if extractor is None:
                _errors.append((name, "no NEWSPAPER attribute"))
                continue
            with _lock:
                _loaded[extractor.display_name] = extractor
        except Exception:
            _errors.append((name, traceback.format_exc(limit=3)))
    _done.set()


def start_background_load() -> None:
    """Begin importing the plugins without blocking the caller."""
    global _thread
    with _lock:
        if _thread is not None:
            return
        _thread = threading.Thread(target=_load_now, daemon=True,
                                   name="newspaper-loader")
        _thread.start()


def load_all(log: Optional[Callable[[str, str], None]] = None
             ) -> Dict[str, type]:
    """Every newspaper extractor, waiting for the background load to finish.

    Safe to call repeatedly; the import work happens once."""
    start_background_load()
    _done.wait(timeout=60)
    if log is not None:
        for name, problem in _errors:
            log(f"[Plugin] {name} failed to load: "
                f"{problem.strip().splitlines()[-1]}", "error")
    with _lock:
        return dict(_loaded)


def errors() -> List[Tuple[str, str]]:
    return list(_errors)
