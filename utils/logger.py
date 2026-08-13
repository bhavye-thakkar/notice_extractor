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
#: Writes are buffered and flushed at most this often (see log()).
FLUSH_EVERY_SECONDS = 0.5
_last_flush = [0.0]
#: Set by close(final=True) when the app is quitting.  close() alone only
#: drops the handle, and the very next log() call reopens it - recreating
#: data/logs milliseconds after shutdown deleted it.  A browser session or a
#: cancelled agent winding down is exactly the thread that does that, so the
#: app's own "nothing is kept" promise was losing a race it did not know it
#: was in.
_shut_down = threading.Event()


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
    if not config.LOG_TO_FILE or _shut_down.is_set():
        return
    try:
        stamp = time.strftime("%H:%M:%S")
        tag = level.upper().ljust(7)
        with _lock:
            log_path()
            assert _handle is not None
            for line in (message or "").splitlines() or [""]:
                _handle.write(f"{stamp} {tag} {line}\n")
            # Flushed on a timer, not per line: this runs on the Tk thread
            # for every line the GUI shows, and a syscall per line during a
            # run is a lag the user feels on every click.  Half a second
            # still means "tail the file while it runs".
            now = time.monotonic()
            if now - _last_flush[0] >= FLUSH_EVERY_SECONDS:
                _handle.flush()
                _last_flush[0] = now
    except Exception:
        pass


def banner(text: str) -> None:
    log("=" * 70)
    log(text)
    log("=" * 70)


def flush() -> None:
    """Force buffered lines out (used before reading the file back)."""
    with _lock:
        if _handle is not None:
            try:
                _handle.flush()
            except Exception:
                pass


def close(final: bool = False) -> None:
    """Close the log file.

    `final=True` also stops it being reopened, which is what shutdown needs:
    log() calls log_path(), and log_path() recreates data/logs on demand, so
    one late line from a thread still winding down puts the folder straight
    back after clear_run_data() removed it.

    Only the real quit path passes final=True.  config.clear_run_data() does
    not - it is also a utility the tests call, and poisoning logging for the
    rest of the process would be a trap for whatever ran next."""
    global _handle
    if final:
        _shut_down.set()
    with _lock:
        if _handle is not None:
            try:
                _handle.close()
            finally:
                _handle = None


def reopen() -> None:
    """Undo close(final=True).  For tests that quit an app and keep going."""
    _shut_down.clear()


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
