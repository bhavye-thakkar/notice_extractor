"""OCR backends.  See ocr/engine.py for the selection order."""

from .engine import (BaseOcrEngine, OcrWord, select_ocr_engine,  # noqa: F401
                     validate_ocr_setup)
