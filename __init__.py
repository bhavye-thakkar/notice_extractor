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

__version__ = "1.36"
