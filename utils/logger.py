"""Run log - the same lines the Status Log shows, written to a file.

The GUI log is a Text widget: it is trimmed while a run is going and gone when
the app closes, which is no use for "run it, then read what happened".  Every
line therefore also goes to `data/logs/run-YYYY-MM-DD.log`, timestamped, from
whichever thread produced it.

Deliberately not the `logging` module: this has one sink, one format and no
configuration, and wiring `logging` up to the Tk queue would be more code than
the whole file.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date, timedelta
from typing import Optional, TextIO

from .. import config

_lock = threading.Lock()
_handle: Optional[TextIO] = None
_path: str = ""


def log_path() -> str:
    """Today's log file (opening it if this is the first line of the run)."""
    global _handle, _path
    if _handle is None:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        _path = os.path.join(config.LOG_DIR,
                             f"run-{date.today().isoformat()}.log")
        _handle = open(_path, "a", encoding="utf-8", errors="replace")
    return _path


def log(message: str, level: str = "info") -> None:
    """Append one line.  Never raises - a failed log write must not kill a
    run that is otherwise working."""
    if not config.LOG_TO_FILE:
        return
    try:
        stamp = time.strftime("%H:%M:%S")
        tag = level.upper().ljust(7)
        with _lock:
            log_path()
            assert _handle is not None
            for line in (message or "").splitlines() or [""]:
                _handle.write(f"{stamp} {tag} {line}\n")
            _handle.flush()
    except Exception:
        pass


def banner(text: str) -> None:
    log("=" * 70)
    log(text)
    log("=" * 70)


def close() -> None:
    global _handle
    with _lock:
        if _handle is not None:
            try:
                _handle.close()
            finally:
                _handle = None


def prune(days: int = 0) -> int:
    """Delete logs older than `days` (default from config).  Returns how many
    were removed."""
    days = days or config.LOG_KEEP_DAYS
    cutoff = date.today() - timedelta(days=days)
    removed = 0
    try:
        for name in os.listdir(config.LOG_DIR):
            if not (name.startswith("run-") and name.endswith(".log")):
                continue
            try:
                stamp = date.fromisoformat(name[4:-4])
            except ValueError:
                continue
            if stamp < cutoff:
                try:
                    os.remove(os.path.join(config.LOG_DIR, name))
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


def demo() -> None:
    log("logger self-check", "info")
    path = log_path()
    close()
    with open(path, "r", encoding="utf-8") as fh:
        assert "logger self-check" in fh.read()
    print(f"logger self-check OK -> {path}")


if __name__ == "__main__":
    demo()
