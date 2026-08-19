"""Sandesh - page discovery, detection pipeline and extractor.

One file per newspaper.
The package loader (scrapers/__init__.py) imports this module in a
background thread and reads NEWSPAPER for the registry entry.
"""

from __future__ import annotations

from notice_extractor.core import *  # noqa: F401,F403  - shared infrastructure


# =============================================================================
# 6b. SANDESH - PAGE DISCOVERY (official web API)
# =============================================================================
# sandesh.com/epaper is a client-rendered React app whose server HTML contains
# no images at all.  The app's real data source - read straight out of its own
# JS bundle (/static/js/main.<hash>.js, api config section "7789") - is a
# public JSON API:
#
#   GET https://new-wapi.sandesh.com/api/v1/e-paper?slug=<edition>&date=YYYY-MM-DD
#
# The response's data.sub[] lists every page IN ORDER as photo paths relative
# to the e-paper CDN, https://epapercdn.sandesh.com/.  A trailing "?w=<px>"
# on each photo selects the rendered width; we ask for a wide render so the
# template matching sees crisp print (the source scans are ~2300 px wide).
# Edition slugs come from .../api/v1/menu/e-paper-menu (ahmedabad,
# ahmedabad-east, gandhinagar, surat, rajkot, vadodara, bhavnagar, bhuj, ...).

SANDESH_API_EPAPER = "https://new-wapi.sandesh.com/api/v1/e-paper"
SANDESH_CDN_BASE = "https://epapercdn.sandesh.com/"
#: Menu endpoint listing every published edition slug (read at run time so a
#: new supplement does not need a code change - see sandesh_editions()).
SANDESH_API_MENU = "https://new-wapi.sandesh.com/api/v1/menu/e-paper-menu"

# The "?w=<px>" the e-paper's own JS appends is IGNORED by the CDN: measured
# 2026-08-19 on page 1 of the Ahmedabad edition, w=300, w=800, w=1600, w=2400
# and no parameter at all every returned the SAME 1,656,142 bytes - the
# full-resolution original (2332x3231).  Two consequences, both of them bugs
# that were live in production:
#
#   * there is no such thing as a Sandesh "preview render".  The fallback that
#     logged "Full-size image unavailable; falling back to the preview render"
#     was re-downloading the identical file under a different URL string - a
#     second 1.6 MB fetch, a second cache entry, and a log line that blamed
#     the newspaper for what was actually a local disk error.
#   * asking for a width was pure cache fragmentation: the same page cached
#     twice because "?w=1600" and "?w=300" are different keys.
#
# So: ONE canonical URL per page, no width parameter, no thumb variant.

SANDESH_URL_PATTERN = re.compile(
    r"^https?://(?:www\.)?sandesh\.com/epaper/"
    r"(?P<edition>[a-z0-9\-]+)"
    r"(?:\?(?:[^#]*&)?date=(?P<date>\d{4}-\d{2}-\d{2})[^#]*)?$",
    re.IGNORECASE)


def sandesh_parse_url(url: str) -> Tuple[str, str]:
    """Validate a Sandesh URL and return (edition, date 'YYYY-MM-DD')."""
    match = SANDESH_URL_PATTERN.match(url.strip())
    if not match:
        raise ExtractionError(
            "The URL is not a recognized Sandesh e-paper URL.\nExpected: "
            "https://sandesh.com/epaper/<edition>?date=YYYY-MM-DD&page=1")
    edition = match.group("edition").lower()
    date_str = match.group("date") or date.today().isoformat()
    return edition, date_str


def _sandesh_http(url: str, timeout: int = 20) -> Tuple[Optional[str], str]:
    """GET the Sandesh API (proxy-aware, with retries).  Returns
    (text or None, human-readable error) - error is '' on success."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://sandesh.com",
        "Referer": "https://sandesh.com/",
    }
    opener = urllib.request.build_opener(build_proxy_handler())
    last = ""
    for attempt in range(1 + HTTP_RETRIES):
        try:
            request = urllib.request.Request(url, headers=headers)
            with opener.open(request, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace"), ""
        except urllib.error.HTTPError as exc:
            last = f"the server replied HTTP {exc.code}"
            if exc.code in (401, 403, 404):
                break
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            text = str(reason).lower()
            if "proxy" in text:
                last = f"proxy error ({reason})"
            elif "name or service" in text or "getaddrinfo" in text or \
                    "nodename" in text:
                last = ("cannot resolve new-wapi.sandesh.com (DNS/proxy) - "
                        "the office network may be blocking it")
            elif "timed out" in text or isinstance(reason, TimeoutError):
                last = "the connection timed out"
            elif "certificate" in text or "ssl" in text:
                last = f"an SSL/certificate problem ({reason})"
            else:
                last = str(reason)
        except Exception as exc:
            last = str(exc)
        time.sleep(HTTP_RETRY_DELAY_SECONDS)
    return None, last


def _sandesh_page_url(photo: str) -> str:
    """The one canonical CDN URL for a page photo path (see the note above:
    the CDN serves the full-resolution original whatever ?w= says)."""
    return f"{SANDESH_CDN_BASE}{photo.split('?')[0].lstrip('/')}"


def _sandesh_photos_from_payload(text: str
                                 ) -> Tuple[List[str], str, str]:
    """Parse the e-paper API JSON.  Returns (photo paths in page order,
    whole-edition PDF path or '', API message).

    Small editions (Gandhinagar) are published as ONE PDF in data.main with
    only the cover in data.sub - photos alone would scan just page 1."""
    try:
        payload = json.loads(text)
    except ValueError:
        return [], "", "response was not JSON"
    if not isinstance(payload, dict):
        return [], "", "unexpected response shape"
    data = payload.get("data")
    subs = data.get("sub") if isinstance(data, dict) else None
    photos: List[str] = []
    if isinstance(subs, list):
        for entry in subs:
            photo = entry.get("photo") if isinstance(entry, dict) else None
            if isinstance(photo, str) and photo.strip():
                photos.append(photo.strip())
    pdf_path = ""
    mains = data.get("main") if isinstance(data, dict) else None
    if isinstance(mains, list):
        for entry in mains:
            pdf = entry.get("pdf") if isinstance(entry, dict) else None
            if isinstance(pdf, str) and pdf.strip().lower().endswith(".pdf"):
                pdf_path = pdf.strip()
                break
    return photos, pdf_path, str(payload.get("message") or "")


#: Editions the app offers when the menu cannot be read (office network
#: down, API moved).  The Ahmedabad group, which is what is monitored for
#: notices - the live list from sandesh_editions() supersedes it.
SANDESH_FALLBACK_EDITIONS: Tuple[str, ...] = (
    "ahmedabad", "ahmedabad-east", "city-life",
    "zalawad---ahmedabad-dist", "gandhinagar", "kheda", "mehsana",
    "sabarkantha", "patan", "banaskantha", "ahmedabad-special-edition",
)
#: Which top-level menu group the app extracts.  Sandesh publishes Surat,
#: Rajkot, Vadodara ... too; the notices being monitored are the Ahmedabad
#: ones, and offering 40 editions nobody scans is not a feature.
SANDESH_MENU_GROUP = "ahmedabad"
#: (editions, when) - the menu is fetched at most once per process.
_EDITIONS_CACHE: List[Tuple[str, ...]] = []


def _menu_slugs(payload: str, group: str) -> Tuple[str, ...]:
    """Edition slugs of one menu group, in the order the site lists them.

    The slug the e-paper API wants is the entry's "category" - NOT its
    "name" ("Ahmedabad City" -> "ahmedabad") and not the top-level group's
    own category ("Ahmedabad" -> "ahmedabad-city", which is a different
    edition).  Both mistakes produce a plausible URL that 404s at run time,
    so the parse is written against the real payload shape."""
    try:
        data = json.loads(payload)
    except ValueError:
        return ()
    data = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(data, dict):
        return ()
    slugs: List[str] = []
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip().lower()
        if group and group not in name:
            continue
        submenu = entry.get("submenu")
        items = submenu.values() if isinstance(submenu, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("category") or "").strip().lower()
            if slug and slug not in slugs:
                slugs.append(slug)
        if slugs:
            break                     # the group we wanted
    return tuple(slugs)


def sandesh_editions() -> Tuple[str, ...]:
    """Every edition Sandesh currently publishes in the monitored group.

    Read from the site's own menu so a new supplement needs no code change;
    falls back to the known list when the network says no.  Cached for the
    life of the process - this is a dropdown, not a feed."""
    if _EDITIONS_CACHE:
        return _EDITIONS_CACHE[0]
    text, _error = _sandesh_http(SANDESH_API_MENU, timeout=12)
    slugs = _menu_slugs(text, SANDESH_MENU_GROUP) if text else ()
    if not slugs:
        slugs = SANDESH_FALLBACK_EDITIONS
    _EDITIONS_CACHE.append(slugs)
    return slugs


def sandesh_discover_pages(downloader: PageDownloader, url: str,
                           reporter: ProgressReporter) -> List[PageRef]:
    """One API call returns the whole edition: every page, in order."""
    edition, date_str = sandesh_parse_url(url)
    reporter.log(f"Opening edition: {edition} / {date_str}")
    reporter.check_cancel()

    api_url = f"{SANDESH_API_EPAPER}?slug={edition}&date={date_str}"
    text, error = _sandesh_http(api_url)
    if text is None:
        proxy = load_proxy()
        proxy_note = (f"\nCurrent proxy: {proxy}" if proxy else
                      "\nIf you are on an office/LAN network, set the proxy "
                      "in Tools > Network (Proxy)...")
        raise ExtractionError(
            "Could not reach the Sandesh e-paper API "
            f"(new-wapi.sandesh.com) - {error}." + proxy_note +
            "\n\nCheck the internet connection "
            "and try again.")

    photos, pdf_path, message = _sandesh_photos_from_payload(text)
    if pdf_path and len(photos) <= 1:
        # Whole edition as one PDF, only the cover as a photo (Gandhinagar):
        # scanning the photos would cover page 1 of ~16.  Same machinery as
        # Divya Bhaskar - download the PDF, render every page.
        pdf_url = f"{SANDESH_CDN_BASE}{pdf_path.lstrip('/')}"
        reporter.log("Edition is published as one PDF - downloading it.")
        return pdf_pages_from_web(downloader, pdf_url, reporter)
    if not photos:
        raise ExtractionError(
            f"Sandesh has no e-paper for '{edition}' on {date_str}"
            + (f" ({message})" if message else "")
            + ".\nPick another date or edition and try again.")

    pages: List[PageRef] = []
    for index, photo in enumerate(photos[:SANDESH_MAX_PAGES], start=1):
        pages.append(PageRef(
            page_number=index,
            image_url=_sandesh_page_url(photo),
            # No thumb: it would be the same bytes under a second URL.
            thumb_url=None,
            page_html_url=(f"https://sandesh.com/epaper/{edition}"
                           f"?date={date_str}&page={index}"),
        ))
    reporter.log(f"Edition has {len(pages)} pages.")
    return pages


def sandesh_get_page_image(downloader: PageDownloader, page: PageRef,
                           reporter: ProgressReporter) -> "np.ndarray":
    if page.image_url is None:
        # PDF-published edition (see sandesh_discover_pages): the ref points
        # at the downloaded file, render the page locally.
        return pdf_render_page(page.page_html_url, page.page_number)
    # No preview fallback: Sandesh has only one rendition (see the note at
    # the top of this file), so a failure here is a real failure - a dead
    # link, a network problem or a full disk - and must be reported as one
    # rather than dressed up as a lower-quality success.
    return downloader.fetch_image(page.image_url,
                                  referer=page.page_html_url)


class SandeshPipeline(NoticeDetectionPipeline):
    """Sandesh: its own header sample plus the Gujarat Samachar samples as
    backup (the typefaces are close), with slightly softer thresholds."""
    newspaper_name = "Sandesh"
    default_config = SANDESH_DETECTION_CONFIG
    embedded_prefixes = ("sandesh-", "gs-", "chetavni-")


class SandeshExtractor(BaseNewspaperExtractor):
    """Sandesh e-paper (sandesh.com/epaper/<city>?date=YYYY-MM-DD&page=N)."""

    display_name = "Sandesh"
    days_back_limit = None       # archive allows any past date
    pipeline_cls = SandeshPipeline
    #: Filled from the site's own menu on first use (sandesh_editions()).
    editions = SANDESH_FALLBACK_EDITIONS

    @classmethod
    def available_editions(cls) -> Tuple[str, ...]:
        cls.editions = sandesh_editions()
        return cls.editions

    @classmethod
    def matches(cls, url: str) -> bool:
        return bool(SANDESH_URL_PATTERN.match(url.strip()))

    @classmethod
    def build_url(cls, edition: str, day: "date") -> str:
        return (f"https://sandesh.com/epaper/{edition}"
                f"?date={day.isoformat()}&page=1")

    @classmethod
    def edition_from_url(cls, url: str) -> Optional[str]:
        match = SANDESH_URL_PATTERN.match(url.strip())
        return match.group("edition").lower() if match else None

    def discover(self, downloader, url, reporter):
        return sandesh_discover_pages(downloader, url, reporter)

    def fetch_page(self, downloader, page, reporter):
        return sandesh_get_page_image(downloader, page, reporter)


#: registry entry read by the package loader
NEWSPAPER = SandeshExtractor
