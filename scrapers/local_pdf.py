"""Local PDF files - page discovery, detection pipeline and extractor.

One file per newspaper.
The package loader (scrapers/__init__.py) imports this module in a
background thread and reads NEWSPAPER for the registry entry.
"""

from __future__ import annotations

from notice_extractor.core import *  # noqa: F401,F403  - shared infrastructure
# Private core helpers are not covered by the star import above.
from notice_extractor.core import (
    _HAVE_FITZ)


# =============================================================================
# 6d. LOCAL PDF FILES (downloaded e-papers - e.g. the Divya Bhaskar PDF)
# =============================================================================

# (PDF_MAX_PAGES / PDF_RENDER_WIDTH live in core.py next to the shared
# pdf_* renderers - three plugins use them.)


class PdfPipeline(NoticeDetectionPipeline):
    """Local PDFs can come from any paper, so every embedded header sample
    is loaded (Divya Bhaskar pill + Gujarat Samachar + Sandesh)."""
    newspaper_name = "PDF file"
    default_config = PDF_DETECTION_CONFIG
    embedded_prefixes = None


class LocalPdfExtractor(BaseNewspaperExtractor):
    """A newspaper PDF from disk (e.g. the downloaded Divya Bhaskar
    e-paper).  Every page is rendered and looped through the same
    detection pipeline as the online editions."""

    display_name = "PDF File"
    days_back_limit = None
    pipeline_cls = PdfPipeline
    debug_on_zero = True

    @classmethod
    def matches(cls, url: str) -> bool:
        stripped = url.strip()
        if re.match(r"^https?://", stripped, re.IGNORECASE) and \
                stripped.split("?", 1)[0].lower().endswith(".pdf"):
            return True
        return pdf_path_from_url(stripped) is not None

    @classmethod
    def build_url(cls, edition: str, day: "date") -> str:
        return ""            # chosen via the 'Open PDF...' button instead

    def discover(self, downloader, url, reporter):
        stripped = url.strip()
        if re.match(r"^https?://", stripped, re.IGNORECASE):
            return pdf_pages_from_web(downloader, stripped, reporter)
        return pdf_discover_pages(stripped, reporter)

    def fetch_page(self, downloader, page, reporter):
        return pdf_render_page(page.page_html_url, page.page_number)


#: registry entry read by the package loader
NEWSPAPER = LocalPdfExtractor
