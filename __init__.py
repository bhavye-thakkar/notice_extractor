"""Public Notice Extractor - finds જાહેર નોટિસ / જાહેર ચેતવણી in Gujarati
e-papers and crops them.

    config.py     paths, the run date, browser and log settings
    core.py       downloader, detection pipeline, main window
    scrapers/     one module per newspaper (+ the headless browser session)
    agents/       the parallel per-edition extraction pipeline
    ocr/          OCR backend selection
    ui/           application entry point and the status log
    utils/        run logging and the notice text search
    data/         saved sessions, run logs, browser profile, diagnostics

Start it with  python notice_extractor/main.py  (or the launcher in the
project root).
"""

import sys as _sys

__version__ = "1.37"


def _use_utf8_console() -> None:
    """Make stdout/stderr able to carry this app's own text.

    Everything here is named in Gujarati - the notice types, the OCR output,
    the keywords a traceback may quote - and Windows still hands a redirected
    or piped stream the ANSI codepage (cp1252).  Printing જાહેર નોટિસ into
    that raises UnicodeEncodeError and kills the process at the print, which
    is how `qa_run.py > report.txt` died on its eighth line with the run
    still going.

    Done in the package __init__ because it is the one thing EVERY door goes
    through - launcher, `python -m`, and each tool in tools/ - and the guard
    is worthless if a single entry point forgets it.  errors="replace" rather
    than "strict": a console that cannot draw Gujarati should print boxes,
    never take the run down with it.
    """
    for stream in (_sys.stdout, _sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower()
        if encoding.replace("-", "") in ("utf8", "utf8mb4"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass          # not a real stream (pythonw, a capture object)


_use_utf8_console()
