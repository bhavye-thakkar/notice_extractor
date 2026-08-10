"""Gujarat Samachar - page discovery, detection pipeline and extractor.

One file per newspaper.
The package loader (scrapers/__init__.py) imports this module in a
background thread and reads NEWSPAPER for the registry entry.
"""

from __future__ import annotations

from notice_extractor.core import *  # noqa: F401,F403  - shared infrastructure


# =============================================================================
# 6. GUJARAT SAMACHAR - PAGE DISCOVERY
# =============================================================================

GS_URL_PATTERN = re.compile(
    r"^https?://epaper\.gujaratsamachar\.com/"
    r"(?P<edition>[a-z0-9\-]+)/(?P<date>\d{2}-\d{2}-\d{4})(?:/(?P<page>\d+))?/?$",
    re.IGNORECASE)

GS_STATIC_IMAGE_PATTERN = re.compile(
    r"https?://[a-z0-9.\-]*epaperstatic\.gujaratsamachar\.com/epaper/"
    r"(?!thumbnail/)[^\s\"'<>]+?\.(?:jpg|jpeg|png)",
    re.IGNORECASE)


class _EditionPageParser(HTMLParser):
    """Parses an edition HTML page.

    Gujarat Samachar's page HTML contains, for the whole edition:
      * one <a href=".../<edition>/<date>/<N>"> per page, wrapping an
        <img src="https://epaperstatic.../epaper/thumbnail/<file>.jpg">
      * pagination links for every page number
      * the current page's full-resolution image
        (https://epaperstatic.../epaper/<file>.jpg  - no /thumbnail/)

    The full-resolution image of ANY page is simply its thumbnail URL with
    the "/thumbnail/" path segment removed.
    """

    def __init__(self, edition: str, date_str: str):
        super().__init__(convert_charrefs=True)
        self._href_re = re.compile(
            r"epaper\.gujaratsamachar\.com/%s/%s/(\d+)/?$"
            % (re.escape(edition), re.escape(date_str)), re.IGNORECASE)
        self.page_thumbs: Dict[int, str] = {}
        self.page_numbers: set = set()
        self.main_images: List[str] = []
        self._current_anchor_page: Optional[int] = None

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "a":
            href = attr.get("href", "") or ""
            match = self._href_re.search(href)
            if match:
                page_no = int(match.group(1))
                self.page_numbers.add(page_no)
                self._current_anchor_page = page_no
            else:
                self._current_anchor_page = None
        elif tag == "img":
            src = attr.get("src") or attr.get("data-src") or ""
            if "/epaper/thumbnail/" in src:
                if self._current_anchor_page is not None:
                    self.page_thumbs.setdefault(self._current_anchor_page, src)
            elif GS_STATIC_IMAGE_PATTERN.match(src):
                self.main_images.append(src)

    def handle_endtag(self, tag):
        if tag == "a":
            self._current_anchor_page = None


def gs_parse_url(url: str) -> Tuple[str, str]:
    """Validate a Gujarat Samachar URL and return (edition, date)."""
    match = GS_URL_PATTERN.match(url.strip())
    if not match:
        raise ExtractionError(
            "The URL is not a recognized Gujarat Samachar e-paper URL.\n"
            "Expected format: https://epaper.gujaratsamachar.com/"
            "<edition>/DD-MM-YYYY/<page>")
    return match.group("edition").lower(), match.group("date")


def gs_thumbnail_to_full(url: str) -> str:
    return url.replace("/epaper/thumbnail/", "/epaper/")


def gs_discover_pages(downloader: PageDownloader, url: str,
                      reporter: ProgressReporter) -> List[PageRef]:
    """Discover every page of the edition from the first page's HTML."""
    edition, date_str = gs_parse_url(url)
    base = f"https://epaper.gujaratsamachar.com/{edition}/{date_str}"

    reporter.log(f"Opening edition: {edition} / {date_str}")
    html = downloader.fetch_text(f"{base}/1")

    parser = _EditionPageParser(edition, date_str)
    try:
        parser.feed(html)
    except Exception as exc:                      # malformed HTML: keep going
        reporter.log(f"HTML parse warning: {exc}", "warn")

    if parser.page_numbers:
        total = max(parser.page_numbers)
    elif parser.page_thumbs:
        total = max(parser.page_thumbs)
    else:
        raise ExtractionError(
            "Could not determine the page list from the edition HTML. "
            "The website layout may have changed - the page-discovery logic "
            "in gs_discover_pages() may need updating.")

    pages: List[PageRef] = []
    for page_no in range(1, total + 1):
        thumb = parser.page_thumbs.get(page_no)
        pages.append(PageRef(
            page_number=page_no,
            image_url=gs_thumbnail_to_full(thumb) if thumb else None,
            thumb_url=thumb,
            page_html_url=f"{base}/{page_no}",
        ))

    with_urls = sum(1 for p in pages if p.image_url)
    reporter.log(f"Edition has {total} pages "
                 f"({with_urls} resolved directly from the index).")
    return pages


def gs_get_page_image(downloader: PageDownloader, page: PageRef,
                      reporter: ProgressReporter) -> "np.ndarray":
    """Fetch one page at the highest available resolution, with fallbacks:
    1) full-res URL derived from the thumbnail,
    2) full-res URL parsed from the page's own HTML,
    3) the thumbnail itself (last resort)."""
    referer = page.page_html_url

    if page.image_url:
        try:
            return downloader.fetch_image(page.image_url, referer=referer)
        except ExtractionError as exc:
            reporter.log(f"  Full-size image unavailable ({exc}); "
                         "trying the page HTML...", "warn")

    # Fallback: parse this page's own HTML for its main image.
    try:
        html = downloader.fetch_text(page.page_html_url)
        match = GS_STATIC_IMAGE_PATTERN.search(html)
        if match:
            return downloader.fetch_image(match.group(0), referer=referer)
    except ExtractionError as exc:
        reporter.log(f"  Page HTML fallback failed: {exc}", "warn")

    if page.thumb_url:
        reporter.log("  Falling back to the thumbnail image "
                     "(reduced quality).", "warn")
        return downloader.fetch_image(page.thumb_url, referer=referer)

    raise ExtractionError(f"No usable image found for page {page.page_number}")


class GujaratSamacharPipeline(NoticeDetectionPipeline):
    """Gujarat Samachar: high-quality embedded templates -> strict
    thresholds (true notices score 0.90+; page furniture stays below)."""
    newspaper_name = "Gujarat Samachar"
    default_config = GS_DETECTION_CONFIG
    embedded_prefixes = ("gs-",)


class GujaratSamacharExtractor(BaseNewspaperExtractor):
    """Gujarat Samachar e-paper (epaper.gujaratsamachar.com)."""

    display_name = "Gujarat Samachar"
    days_back_limit = 7          # the online archive keeps the last 7 days
    pipeline_cls = GujaratSamacharPipeline

    @classmethod
    def matches(cls, url: str) -> bool:
        return bool(GS_URL_PATTERN.match(url.strip()))

    @classmethod
    def build_url(cls, edition: str, day: "date") -> str:
        return (f"https://epaper.gujaratsamachar.com/{edition}/"
                f"{day.day:02d}-{day.month:02d}-{day.year}/1")

    @classmethod
    def edition_from_url(cls, url: str) -> Optional[str]:
        match = GS_URL_PATTERN.match(url.strip())
        return match.group("edition").lower() if match else None

    def discover(self, downloader, url, reporter):
        return gs_discover_pages(downloader, url, reporter)

    def fetch_page(self, downloader, page, reporter):
        return gs_get_page_image(downloader, page, reporter)


#: registry entry read by the package loader
NEWSPAPER = GujaratSamacharExtractor
