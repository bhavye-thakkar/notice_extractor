"""Nav Gujarat Samay - page discovery, detection pipeline and extractor.

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
# 6e. NAV GUJARAT SAMAY (Readwhere reader - epaper.navgujaratsamay.com)
# =============================================================================

NGS_HOST = "https://epaper.navgujaratsamay.com"
NGS_MAX_PAGES = 40
NGS_URL_RE = re.compile(
    r"^https?://(?:www\.)?epaper\.navgujaratsamay\.com/reader/"
    r"(?P<issue>\d+)/(?P<edition>[A-Za-z0-9\-]+)/"
    r"(?P<date>\d{2}-[A-Za-z]{3}-\d{4})(?:/page/(?P<page>\d+))?"
    r"(?:/\d+)?/?(?:[?#].*)?$", re.IGNORECASE)
_NGS_TOTAL_RE = re.compile(r'id="totalPages"[^>]*>\s*(\d+)', re.IGNORECASE)
_NGS_THUMB_RE = re.compile(
    r"https?://[^\s\"'<>\\]*?/resourcethumb/(?P<hash>[A-Za-z0-9\-]+)"
    r"_(?P<size>\d+)\.jpg", re.IGNORECASE)
_NGS_BIG_IMG_RE = re.compile(
    r"https?://[^\s\"'<>\\]+?\.(?:jpg|jpeg|png|webp)(?:\?[^\s\"'<>\\]*)?",
    re.IGNORECASE)
# Render widths the Readwhere thumb service actually serves (larger 404).
NGS_THUMB_SIZES: Tuple[int, ...] = (600, 300, 150)
NGS_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def ngs_parse_url(url: str) -> Tuple[str, str, str, int]:
    """(issue id, edition, ISO date, page) from a reader URL."""
    match = NGS_URL_RE.match(url.strip())
    if not match:
        raise ExtractionError(
            "The URL is not a recognized Nav Gujarat Samay reader URL.\n"
            "Expected: https://epaper.navgujaratsamay.com/reader/"
            "<issueId>/Ahmedabad/08-AUG-2026/page/1/1")
    raw_date = match.group("date").upper()
    day, mon, year = raw_date.split("-")
    month = NGS_MONTHS.index(mon) + 1 if mon in NGS_MONTHS else 1
    iso = f"{int(year):04d}-{month:02d}-{int(day):02d}"
    return (match.group("issue"), match.group("edition"), iso,
            int(match.group("page") or 1))


def ngs_reader_url(issue: str, edition: str, iso_date: str,
                   page: int = 1) -> str:
    day = date.fromisoformat(iso_date)
    stamp = f"{day.day:02d}-{NGS_MONTHS[day.month - 1]}-{day.year}"
    return f"{NGS_HOST}/reader/{issue}/{edition}/{stamp}/page/{page}/1"


NGS_PAGEMETA = (NGS_HOST + "/reader/download/pagemeta/get/newspaper/"
                "{issue}/1-100")
# The reader's calendar feed: which issue id was published on which day.
NGS_PUBLISHDATES = (NGS_HOST + "/reader/publishdates/{title}/{frm}/{to}"
                    "/json")
# Edition -> titleId (read off the live reader; more can be learned from a
# pasted URL at runtime).
NGS_TITLE_IDS: Dict[str, str] = {
    "ahmedabad": "26717",
}
NGS_DEFAULT_TITLE_ID = "26717"
#: resolved {(edition, iso date): issue id} cache for this session
_ngs_issue_cache: Dict[Tuple[str, str], str] = {}


def ngs_title_id(edition: str) -> str:
    return NGS_TITLE_IDS.get(edition.lower(), NGS_DEFAULT_TITLE_ID)


def ngs_resolve_issue(downloader: PageDownloader, edition: str,
                      iso_date: str,
                      reporter: ProgressReporter) -> Optional[str]:
    """The issue id published on `iso_date` for this edition, via the
    reader's own calendar feed.  None when that day has no edition."""
    key = (edition.lower(), iso_date)
    if key in _ngs_issue_cache:
        return _ngs_issue_cache[key]
    day = date.fromisoformat(iso_date)
    first = datetime.datetime(day.year, day.month, 1)
    frm = int(first.timestamp()) - 86400
    to = int((first + timedelta(days=31)).timestamp())
    url = NGS_PUBLISHDATES.format(title=ngs_title_id(edition), frm=frm,
                                  to=to)
    try:
        text = downloader.fetch_text(url, referer=NGS_HOST + "/")
    except ExtractionError as exc:
        reporter.log(f"  Could not read the Nav Gujarat Samay calendar "
                     f"({exc}).", "warn")
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, list):
        return None
    found: Optional[str] = None
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        published = str(entry.get("published")
                        or entry.get("published_date")
                        or entry.get("date") or "")[:10]
        issue = str(entry.get("id") or "").strip()
        if published and issue:
            _ngs_issue_cache[(edition.lower(), published)] = issue
            if published == iso_date:
                found = issue
    return found


def ngs_page_links(downloader: PageDownloader, issue: str,
                   reporter: ProgressReporter) -> Dict[int, str]:
    """{page number: absolute per-page PDF link} from the reader's own
    page-metadata endpoint (public - no login needed)."""
    meta_url = NGS_PAGEMETA.format(issue=issue)
    try:
        text = downloader.fetch_text(meta_url, referer=NGS_HOST + "/")
    except ExtractionError as exc:
        raise ExtractionError(
            "Could not read the Nav Gujarat Samay page list "
            f"({exc}).")
    try:
        payload = json.loads(text)
    except ValueError:
        raise ExtractionError(
            "The Nav Gujarat Samay page list was not JSON - the site may "
            "have changed.")
    links: Dict[int, str] = {}
    if isinstance(payload, dict):
        for key, entry in payload.items():
            if not isinstance(entry, dict):
                continue
            link = entry.get("download_link")
            try:
                number = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(link, str) and link.strip():
                links[number] = urllib.parse.urljoin(NGS_HOST,
                                                     link.strip())
    return links


def ngs_discover_pages(downloader: PageDownloader, url: str,
                       reporter: ProgressReporter) -> List[PageRef]:
    issue, edition, iso_date, _page = ngs_parse_url(url)
    if issue in ("0", "", None):
        reporter.log(f"Opening edition: {edition} / {iso_date}")
        reporter.check_cancel()
        resolved = ngs_resolve_issue(downloader, edition, iso_date, reporter)
        if not resolved:
            raise ExtractionError(
                f"Nav Gujarat Samay published no {edition} edition on "
                f"{iso_date} (or the calendar could not be read).\n"
                "Pick another date, or paste that day's reader URL.")
        issue = resolved
        reporter.log(f"  Resolved issue id {issue} for {iso_date}.", "dim")
    else:
        reporter.log(f"Opening edition: {edition} / {iso_date} "
                     f"(issue {issue})")
    reporter.check_cancel()
    links = ngs_page_links(downloader, issue, reporter)
    if not links:
        raise ExtractionError(
            "Nav Gujarat Samay returned no pages for this issue.\n"
            "Open the edition in your browser and paste its reader URL "
            "(the issue id is per edition and per day).")
    numbers = sorted(links)[:NGS_MAX_PAGES]
    reporter.log(f"Edition has {len(numbers)} pages.")
    return [PageRef(page_number=number, image_url=links[number],
                    thumb_url=None,
                    page_html_url=ngs_reader_url(issue, edition, iso_date,
                                                 number))
            for number in numbers]


def _ngs_page_candidates(html: str, page_url: str) -> List[str]:
    """Page-render URLs for one reader page, best (largest) first."""
    candidates: List[str] = []
    # resourcethumb hashes: ask for the largest size the service serves.
    for match in _NGS_THUMB_RE.finditer(html.replace("\\/", "/")):
        stem = match.group(0).rsplit("_", 1)[0]
        for size in NGS_THUMB_SIZES:
            candidate = f"{stem}_{size}.jpg"
            if candidate not in candidates:
                candidates.append(candidate)
    # any other large-looking image on the page (skip UI furniture).
    for match in _NGS_BIG_IMG_RE.finditer(html.replace("\\/", "/")):
        raw = match.group(0)
        low = raw.lower()
        if any(bad in low for bad in ("logo", "icon", "favicon", "masthead",
                                      "sprite", "banner", "avatar",
                                      "transparent", "placeholder")):
            continue
        if raw not in candidates:
            candidates.append(raw)
    return candidates


def ngs_get_page_image(downloader: PageDownloader, page: PageRef,
                       reporter: ProgressReporter) -> "np.ndarray":
    """One Nav Gujarat Samay page.  The reader's own per-page link serves a
    PDF, which is rendered at print width; anything else is treated as an
    image, and the old HTML scan stays as a last resort."""
    if page.image_url:
        try:
            data = downloader.fetch_bytes(page.image_url,
                                          referer=page.page_html_url)
        except ExtractionError:
            data = b""
        if data.startswith(b"%PDF"):
            if not _HAVE_FITZ:
                raise ExtractionError(
                    "Nav Gujarat Samay pages arrive as PDF - press "
                    "'Download Dependencies' once (it installs pymupdf) "
                    "and restart.")
            local = downloader._cache_path(page.image_url) + ".pdf"
            try:
                with open(local, "wb") as fh:
                    fh.write(data)
                return pdf_render_page(local, 1)
            except ExtractionError:
                raise
            except Exception as exc:
                raise ExtractionError(
                    f"Could not render that Nav Gujarat Samay page: {exc}")
        if data:
            array = np.frombuffer(data, dtype=np.uint8)
            image = cv2.imdecode(array, cv2.IMREAD_COLOR)
            if image is not None:
                height, width = image.shape[:2]
                if width < 1400:
                    factor = min(3, max(2, int(round(1600 / max(1, width)))))
                    image = cv2.resize(image, (width * factor,
                                               height * factor),
                                       interpolation=cv2.INTER_CUBIC)
                return image
    html = downloader.fetch_text(page.page_html_url, referer=NGS_HOST + "/")
    best: Optional["np.ndarray"] = None
    for candidate in _ngs_page_candidates(html, page.page_html_url):
        reporter.check_cancel()
        try:
            image = downloader.fetch_image(candidate,
                                           referer=page.page_html_url)
        except ExtractionError:
            continue
        height, width = image.shape[:2]
        if width < 140 or height <= width:      # not a newspaper page
            continue
        if best is None or width > best.shape[1]:
            best = image
        if width >= 900:                        # good enough, stop early
            break
    if best is None:
        raise ExtractionError(
            "No page image could be resolved for this Nav Gujarat Samay "
            "page.  Their reader loads pages through scripts; if this "
            "keeps happening, share the diagnostic folder saved next to "
            "the program so the pattern can be matched exactly.")
    height, width = best.shape[:2]
    if width < 1400:            # 600 px renders -> upscale for detection
        factor = min(3, max(2, int(round(1600 / max(1, width)))))
        best = cv2.resize(best, (width * factor, height * factor),
                          interpolation=cv2.INTER_CUBIC)
    return best


class NavGujaratSamayPipeline(NoticeDetectionPipeline):
    """Nav Gujarat Samay: bold Gujarati notice headers, so all embedded
    real-paper samples are used with slightly softer thresholds (its
    renders are lower resolution and upscaled)."""
    newspaper_name = "Nav Gujarat Samay"
    default_config = DetectionConfig(
        box_match_threshold=0.64,
        page_match_threshold=0.70,
    )
    embedded_prefixes = None            # every positive sample


class NavGujaratSamayExtractor(BaseNewspaperExtractor):
    """Nav Gujarat Samay e-paper (epaper.navgujaratsamay.com/reader/...).

    The reader's issue id is assigned by the site per edition+date and
    cannot be derived, so the reader URL is pasted from the browser (the
    Edition/Date pickers then reuse that issue's id for its own date)."""

    display_name = "Nav Gujarat Samay"
    days_back_limit = None
    pipeline_cls = NavGujaratSamayPipeline
    editions = ("Ahmedabad", "Gandhinagar")
    default_edition = "Ahmedabad"
    debug_on_zero = True
    #: last issue id seen in a pasted URL (remembered for the pickers)
    last_issue_id: str = ""

    @classmethod
    def matches(cls, url: str) -> bool:
        return bool(NGS_URL_RE.match(url.strip()))

    @classmethod
    def build_url(cls, edition: str, day: "date") -> str:
        """Always produces a URL.  Issue id 0 means "look the issue up for
        this date from the site's calendar" (done during discovery)."""
        cached = _ngs_issue_cache.get((edition.lower(), day.isoformat()))
        return ngs_reader_url(cached or "0", edition, day.isoformat(), 1)

    @classmethod
    def edition_from_url(cls, url: str) -> Optional[str]:
        try:
            issue, edition, _iso, _page = ngs_parse_url(url)
        except ExtractionError:
            return None
        cls.last_issue_id = issue
        return edition

    def __init__(self, broad: bool = False):
        super().__init__(broad=broad)
        self._first_signature: Optional[str] = None

    def discover(self, downloader, url, reporter):
        try:
            issue, _ed, iso, _pg = ngs_parse_url(url)
            type(self).last_issue_id = issue
            self.current_issue_date = iso
        except ExtractionError:
            pass
        self._first_signature = None
        return ngs_discover_pages(downloader, url, reporter)

    def fetch_page(self, downloader, page, reporter):
        image = ngs_get_page_image(downloader, page, reporter)
        # Safety net: if the site ever falls back to serving one shared
        # render for every page, say so once instead of scanning it N times.
        signature = hashlib.md5(image.tobytes()).hexdigest()
        if page.page_number == 1:
            self._first_signature = signature
        elif signature == self._first_signature:
            raise ExtractionError(
                "AUTH: Nav Gujarat Samay served the same image for page "
                f"{page.page_number} as for page 1, so the real pages are "
                "not being delivered.\n\nOpen the edition in your browser "
                "and paste its reader URL again (the issue id changes per "
                "day), or use 'Open PDF...'.")
        return image


#: registry entry read by the package loader
NEWSPAPER = NavGujaratSamayExtractor
