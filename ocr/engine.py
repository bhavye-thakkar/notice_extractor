"""The OCR surface: one import site for every engine the app can use.

Backends are tried in this order and the first that actually reads Gujarati
wins (see core.select_ocr_engine):

    1. Windows.Media.Ocr   - winrt bindings + the Windows Gujarati pack
    2. Tesseract           - tesseract.exe + tessdata/guj.traineddata
    3. EasyOCR             - only if it is already installed

The implementations live in core.py next to the detection pipeline that calls
them; this module is the stable name the rest of the package (and any script)
imports, so the pipeline can move without touching every caller.
"""

from __future__ import annotations

from ..core import (BaseOcrEngine, EasyOcrEngine, OcrWord,  # noqa: F401
                    TesseractOcrEngine, WindowsOcrEngine, get_ocr_pool,
                    reset_ocr_engine_cache, select_ocr_engine,
                    shutdown_ocr_pool, validate_ocr_setup)

__all__ = ["BaseOcrEngine", "EasyOcrEngine", "OcrWord", "TesseractOcrEngine",
           "WindowsOcrEngine", "get_ocr_pool", "reset_ocr_engine_cache",
           "select_ocr_engine", "shutdown_ocr_pool", "validate_ocr_setup"]
