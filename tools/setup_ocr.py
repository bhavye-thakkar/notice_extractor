#!/usr/bin/env python3
"""OCR dependency setup for the Public Notice Extractor.

    python setup_ocr.py              # report what is installed and what is not
    python setup_ocr.py --install    # pip-install the pip-installable parts
    python setup_ocr.py --raqm       # what RAQM is, and why pip cannot fix it

The probe and the installers live in notice_extractor/core.py, so this
script and the app
can never disagree about what "ready" means.

Backend priority (first one that can read Gujarati wins):
    1. Windows built-in OCR  - the winrt bindings + the Gujarati OCR pack
    2. Tesseract             - the Tesseract program + guj.traineddata
    3. EasyOCR               - pip only, but ships no Gujarati model today
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys

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
TARGET = os.path.join(ROOT, "notice_extractor", "core.py")

RAQM_NOTES = """
RAQM  -  Gujarati text SHAPING inside Pillow
============================================
What it does
    Gujarati is a complex script: glyphs reorder and combine (જ + િ renders
    the vowel BEFORE the consonant).  Without a shaping engine Pillow draws
    the code points in logical order, so text rendered by the app looks wrong.

What it actually affects here
    ONLY the extra header templates the app renders from system fonts.
    The embedded base64 templates were shaped correctly when they were built,
    and when OCR is active it is OCR - not template rendering - that decides
    whether a header matches.  So a missing RAQM degrades a fallback, it does
    not break detection.  Getting OCR working matters far more.

Why pip cannot fix it
    There is no 'pillow[raqm]' extra on PyPI - that command fails.  Pillow's
    Windows wheels do not bundle libraqm either (verified against Pillow
    10.4.0, 11.3.0 and 12.3.0), so upgrading Pillow changes nothing.
    RAQM needs libraqm + harfbuzz + fribidi compiled into Pillow.

If you really want it
    Linux :  sudo apt install libraqm-dev libharfbuzz-dev libfribidi-dev
             pip install --no-binary :all: --force-reinstall pillow
    macOS :  brew install libraqm
             pip install --no-binary :all: --force-reinstall pillow
    Windows: no supported binary path; it means building Pillow from source
             against libraqm.  Not worth it - install a real OCR backend
             instead (run this script with no arguments to see how).

Check it with
    python -c "from PIL import features; print(features.check('raqm'))"
"""


def load_app_module():
    from notice_extractor import core
    return core


def pip_install(packages) -> int:
    """Install `packages`, streaming pip's output."""
    if not packages:
        return 0
    command = [sys.executable, "-m", "pip", "install", "--upgrade", *packages]
    print("\n$ " + " ".join(command) + "\n")
    try:
        return subprocess.call(command)
    except OSError as exc:
        print(f"could not run pip: {exc}")
        return 1


def missing_pip_packages(pne) -> list:
    """The pip-installable pieces that are not present yet."""
    wanted = []
    if sys.platform.startswith("win") and \
            pne.WindowsOcrEngine._import_winsdk() is None:
        # NOT plain "winsdk": that project's last release only has wheels up
        # to CPython 3.12, so on 3.13+ pip tries a source build and fails.
        wanted.extend(pne.WINDOWS_OCR_PACKAGES)
    if not pne._HAVE_PYTESSERACT:
        wanted.append("pytesseract")
    return wanted


def main() -> int:
    # This script prints Gujarati; a default Windows console is cp1252 and
    # would raise UnicodeEncodeError partway through the report.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="Check and install the OCR stack.")
    ap.add_argument("--install", action="store_true",
                    help="pip-install the missing pip-installable packages")
    ap.add_argument("--raqm", action="store_true",
                    help="explain the Pillow RAQM shaping situation")
    args = ap.parse_args()

    if args.raqm:
        print(RAQM_NOTES)
        return 0

    if not os.path.isfile(TARGET):
        print(f"cannot find {TARGET}")
        return 2

    print("Public Notice Extractor  -  OCR setup")
    print("=" * 62)
    pne = load_app_module()

    if args.install:
        wanted = missing_pip_packages(pne)
        if not wanted:
            print("\nNothing to pip-install - every pip package is present.")
        else:
            print("\nInstalling: " + ", ".join(wanted))
            if pip_install(wanted) != 0:
                print("\npip reported an error; see the output above.")
                return 1
            # Re-import so the report below reflects what was just installed.
            pne = load_app_module()
        print("\nGujarati language data")
        print("-" * 62)
        pne.ensure_tesseract(lambda line: print(line))
        pne.ensure_gujarati_traineddata(lambda line: print(line))
        pne = load_app_module()

    status, fixes = pne.validate_ocr_setup()

    print("\nBackends")
    print("-" * 62)
    for line in status:
        print("  " + line)

    try:
        from PIL import features
        raqm = bool(features.check("raqm"))
    except Exception:
        raqm = False
    print(f"  Pillow RAQM : {'yes' if raqm else 'no  (see --raqm)'}")

    engine = pne.select_ocr_engine(pne._SilentReporter())
    print("\nVerdict")
    print("-" * 62)
    if engine is not None and engine.supports_gujarati:
        print(f"  READY  -  the app will use {engine.name} for Gujarati.")
        return 0

    print("  NOT READY  -  no backend can read Gujarati, so the app falls")
    print("  back to template-only detection, which finds far fewer notices.")
    if fixes:
        print("\nTo fix, do ONE of these")
        print("-" * 62)
        for line in fixes:
            print("  * " + line)
    print("\n  Easiest on Windows - Tesseract, no admin rights needed after")
    print("  the installer itself:")
    print("    winget install UB-Mannheim.TesseractOCR")
    print("    python setup_ocr.py --install")
    print("  (--install downloads guj.traineddata into "
          + pne.local_tessdata_dir() + ")")
    return 1


if __name__ == "__main__":
    sys.exit(main())
