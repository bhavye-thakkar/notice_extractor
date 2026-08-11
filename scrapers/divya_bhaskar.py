"""Divya Bhaskar - page discovery, detection pipeline and extractor.

One file per newspaper; the package loader (scrapers/__init__.py) imports this
module in a background thread and reads NEWSPAPER for the registry entry.

Discovery is FULLY AUTOMATIC (v1.36).  A headless browser opens

    /epaper/detail-page/<edition>/<edCode>/<YYYY-MM-DD>

and the rendered page hands over everything the download needs:

    props.initialState.common.epaperDetail.pgs        -> the ordered page list
    props.initialState.common.epaperDetail.ss.subsData.pt
                                                      -> the access token

Each page is then pulled over plain HTTP with `pt` in the request header
(verified live: the CDN answers 401 without it, 3600x2244 px with it), which
is far quicker than driving a browser through eighteen pages.  Nothing is
copied out of DevTools, and the browser profile is persistent, so the login it
carries is refreshed on every run instead of going stale.

Older routes (browser cookie store, saved cookie, CDN pattern probing, page
scraping) are kept below as fallbacks for machines without Playwright.
"""

from __future__ import annotations

from notice_extractor.core import *  # noqa: F401,F403  - infrastructure
# Private core helpers are not covered by the star import above.
from notice_extractor.core import _SilentReporter

from .. import config
from .browser_session import BrowserSession, BrowserUnavailable


# =============================================================================
# 6c. DIVYA BHASKAR - PAGE DISCOVERY (best-effort, multi-strategy)
# =============================================================================
# divyabhaskar.co.in/epaper is a client-rendered app (DB Corp) without a
# stable public JSON API, and it sometimes asks for a login.  Discovery is
# therefore multi-strategy and defensive:
#   1. The requested page's HTML is scanned for a Next.js __NEXT_DATA__ blob;
#      page-image lists found inside it are used (sorted by page number when
#      the entries carry one).
#   2. Otherwise every image URL in the HTML that looks like an e-paper page
#      render (bhaskarassets / dainikbhaskar / divyabhaskar hosts with an
#      "epaper" path) is collected in document order.
#   3. If the URL was the viewer root, the best-matching edition link is
#      followed once and steps 1-2 repeat.
# When the viewer requires a login or its layout changed, extraction stops
# with a clear message instead of guessing.

DB_MAX_PAGES = 40

# --- stored login session ----------------------------------------------------
# The premium e-paper sits behind a login.  The user logs in ONCE in the
# browser, copies the session cookie via Tools > Divya Bhaskar Login..., and
# the app then fetches every page as that logged-in user.  The cookie is
# stored next to the program and reused until it expires.
DB_SESSION_FILENAME = "divyabhaskar_session.txt"
_db_session_cache: Optional[str] = None


def db_session_path() -> str:
    return config.session_file(DB_SESSION_FILENAME)


_db_runtime_cookie: Optional[str] = None


def db_set_runtime_cookie(cookie: str) -> None:
    """A per-run cookie (e.g. auto-imported from the browser) that overrides
    the stored one for this extraction only."""
    global _db_runtime_cookie
    _db_runtime_cookie = cookie or None


def db_load_session_cookie(force: bool = False) -> str:
    """The stored session cookie ('' when none is saved)."""
    global _db_session_cache
    if _db_session_cache is None or force:
        cookie = ""
        try:
            with open(db_session_path(), "r", encoding="utf-8") as fh:
                cookie = fh.read().strip()
        except OSError:
            cookie = ""
        _db_session_cache = cookie
    return _db_session_cache


def db_save_session_cookie(cookie: str) -> str:
    """Persist the cookie; returns the file path."""
    global _db_session_cache
    path = db_session_path()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(cookie.strip())
    _db_session_cache = cookie.strip()
    return path


# --- automatic login: read the live cookie from the browser -----------------
# Config file next to the program remembers the chosen browser/profile so the
# import is fully automatic on every later run.
DB_AUTOLOGIN_FILENAME = "divyabhaskar_autologin.json"
DB_COOKIE_DOMAINS = ("divyabhaskar.co.in", "bhaskarassets.com", "bhaskar.com")
# A browser cookie only counts as "logged in" when it carries one of these
# Divya Bhaskar auth tokens (otherwise it is just an anonymous visit).
DB_AUTH_COOKIE_KEYS = ("dbskrat", "pt", "at", "dbskrrt", "dbskruid", "rt")

# Where each supported Chromium browser keeps its profile data (Windows).
DB_BROWSER_ROOTS: Tuple[Tuple[str, str], ...] = (
    ("Chrome",  r"%LOCALAPPDATA%\Google\Chrome\User Data"),
    ("Edge",    r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
    ("Brave",   r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data"),
)


def db_autologin_path() -> str:
    return config.session_file(DB_AUTOLOGIN_FILENAME)


def db_load_autologin() -> Optional[Dict[str, str]]:
    """Auto-login config.  Default (no file) is ON with Chrome, so the app
    reads the live browser login out of the box.  'Turn Off' writes an
    explicit {"enabled": false} marker."""
    default = {"browser": "Auto", "profile": "Default", "enabled": True,
               "is_default": True}
    try:
        with open(db_autologin_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            if data.get("enabled") is False:
                return None                # explicitly turned off
            if data.get("browser"):
                data.setdefault("enabled", True)
                return data
    except (OSError, ValueError):
        pass
    return default


def db_save_autologin(browser: str, profile: str = "Default") -> str:
    path = db_autologin_path()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"browser": browser, "profile": profile,
                   "enabled": True}, fh)
    return path


def db_clear_autologin() -> None:
    # Explicit OFF marker (so the default-ON does not re-enable it).
    try:
        with open(db_autologin_path(), "w", encoding="utf-8") as fh:
            json.dump({"enabled": False}, fh)
        return
    except OSError:
        pass
    try:
        os.remove(db_autologin_path())
    except OSError:
        pass


def _win_dpapi_unprotect(data: bytes) -> bytes:
    """Windows DPAPI decrypt via crypt32 (no pywin32 needed)."""
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf_in = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data),
                        ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out)):
        raise OSError("DPAPI CryptUnprotectData failed")
    try:
        out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return out


def _chromium_master_key(user_data_dir: str) -> Optional[bytes]:
    """The AES key that Chromium used to encrypt its cookies (v10/v11)."""
    local_state = os.path.join(user_data_dir, "Local State")
    try:
        with open(local_state, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        blob = base64.b64decode(state["os_crypt"]["encrypted_key"])
    except (OSError, ValueError, KeyError):
        return None
    if blob[:5] == b"DPAPI":
        blob = blob[5:]
    try:
        return _win_dpapi_unprotect(blob)
    except Exception:
        return None


def _chromium_decrypt_value(enc: bytes, key: Optional[bytes]) -> str:
    """Decrypt one cookie value (handles the v10/v11 AES-GCM scheme and the
    legacy DPAPI scheme)."""
    if not enc:
        return ""
    if enc[:3] in (b"v10", b"v11"):
        if key is None:
            return ""
        try:
            from Crypto.Cipher import AES  # pycryptodome
        except ImportError:
            return ""
        nonce, ciphertext, tag = enc[3:15], enc[15:-16], enc[-16:]
        try:
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ciphertext, tag).decode(
                "utf-8", "ignore")
        except Exception:
            return ""
    try:                                   # legacy DPAPI-encrypted value
        return _win_dpapi_unprotect(enc).decode("utf-8", "ignore")
    except Exception:
        return ""


def _chromium_cookie_db(user_data_dir: str, profile: str) -> Optional[str]:
    for rel in (os.path.join(profile, "Network", "Cookies"),
                os.path.join(profile, "Cookies")):
        path = os.path.join(user_data_dir, rel)
        if os.path.isfile(path):
            return path
    return None


def _read_browser_cookie(browser: str, profile: str,
                         domains: Tuple[str, ...]) -> str:
    """Read + decrypt cookies for the given domains from one browser
    profile.  Returns a 'k=v; k2=v2' header (empty on any failure)."""
    if not sys.platform.startswith("win"):
        return ""
    root = dict((b, os.path.expandvars(p)) for b, p in DB_BROWSER_ROOTS).get(
        browser)
    if not root or not os.path.isdir(root):
        return ""
    db_path = _chromium_cookie_db(root, profile)
    if not db_path:
        return ""
    key = _chromium_master_key(root)
    # The cookie DB is locked while the browser runs - work on a copy.
    tmp = os.path.join(tempfile.gettempdir(),
                       f"pne_cookies_{os.getpid()}.db")
    try:
        shutil.copy2(db_path, tmp)
        import sqlite3
        con = sqlite3.connect(tmp)
        try:
            rows = con.execute(
                "SELECT host_key, name, encrypted_value, value "
                "FROM cookies").fetchall()
        finally:
            con.close()
    except Exception:
        return ""
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    jar: Dict[str, str] = {}
    for host_key, name, enc_value, plain in rows:
        host = (host_key or "").lstrip(".").lower()
        if not any(host == d or host.endswith("." + d) for d in domains):
            continue
        value = plain or _chromium_decrypt_value(enc_value, key)
        if value:
            jar[name] = value
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def _chromium_profiles(user_data_dir: str) -> List[str]:
    """Profile folders in a browser's user-data dir that hold a cookie DB."""
    profiles: List[str] = []
    for name in ("Default",) + tuple(f"Profile {i}" for i in range(1, 12)):
        if _chromium_cookie_db(user_data_dir, name):
            profiles.append(name)
    try:
        for name in sorted(os.listdir(user_data_dir)):
            if name not in profiles and \
                    _chromium_cookie_db(user_data_dir, name):
                profiles.append(name)
    except OSError:
        pass
    return profiles or ["Default"]


def _db_cookie_keys(cookie: str) -> set:
    return {p.split("=", 1)[0].strip().lower()
            for p in (cookie or "").split(";") if "=" in p}


def _db_cookie_is_authed(cookie: str) -> bool:
    return bool(_db_cookie_keys(cookie) & set(DB_AUTH_COOKIE_KEYS))


def _db_cookie_is_logged_in(cookie: str) -> bool:
    """Proof of a real SIGNED-IN reader, not just a visited site.

    The weak test above accepts 'pt', which divyabhaskar.co.in also hands to
    anonymous visitors - trusting it made a fresh profile look logged in and
    skipped the step that would have logged it in.  Only the dbskr* account
    tokens (or the at+rt pair) mean an account."""
    keys = _db_cookie_keys(cookie)
    return bool(keys & {"dbskrat", "dbskrrt", "dbskruid"}) or \
        {"at", "rt"} <= keys


def db_import_browser_cookie(reporter: "ProgressReporter") -> str:
    """Live Divya Bhaskar cookie read straight from the browser.

    In "Auto" mode (the default) every installed browser and every profile
    is scanned and the first LOGGED-IN one wins - so nothing needs to be
    chosen: if the DB tab is open and logged in anywhere, it is used."""
    config = db_load_autologin()
    if not config:
        return ""
    browser = config.get("browser", "")
    auto = config.get("is_default") or browser.lower() in (
        "auto", "any", "all", "")

    if auto:
        targets = [b for b, _root in DB_BROWSER_ROOTS]
    else:
        targets = [browser]

    best_anon = ""
    for name in targets:
        root = dict((b, os.path.expandvars(p))
                    for b, p in DB_BROWSER_ROOTS).get(name)
        if not root or not os.path.isdir(root):
            continue
        for profile in (_chromium_profiles(root) if auto
                        else [config.get("profile", "Default")]):
            cookie = _read_browser_cookie(name, profile, DB_COOKIE_DOMAINS)
            if not cookie:
                continue
            if _db_cookie_is_authed(cookie):
                where = name if profile == "Default" else f"{name}/{profile}"
                reporter.log(f"Auto-imported the current {where} login "
                             "(fresh session).", "success")
                return cookie
            best_anon = best_anon or cookie

    if best_anon:
        reporter.log("A browser cookie was found but it is not logged in to "
                     "Divya Bhaskar premium - log in in the browser, or "
                     "use Open PDF.", "warn")
        return ""
    reporter.log("Auto-login: no logged-in Divya Bhaskar browser session "
                 "found (browser not installed / not logged in, or Chrome's "
                 "app-bound cookie encryption).  Trying the saved cookie / "
                 "use Open PDF.", "warn")
    return ""


def db_probe_browsers() -> List[Tuple[str, bool]]:
    """(browser, installed?) for the Tools dialog."""
    out: List[Tuple[str, bool]] = []
    for browser, root in DB_BROWSER_ROOTS:
        out.append((browser, os.path.isdir(os.path.expandvars(root))))
    return out


def _db_headers() -> Optional[Dict[str, str]]:
    """Auth for one CDN / site request.

    Two independent credentials, both discovered automatically: the session
    cookie, and the `pt` access token the viewer mints per page load.  The CDN
    accepts either; sending both survives one of them going stale."""
    headers: Dict[str, str] = {}
    cookie = _db_runtime_cookie or db_load_session_cookie()
    if cookie:
        headers["Cookie"] = cookie
    if _db_runtime_pt:
        headers["pt"] = _db_runtime_pt
    return headers or None


# =============================================================================
# 6c-1. AUTOMATED SESSION - headless browser, no manual cookie work
# =============================================================================
# The viewer is a Next.js app: the page list and the access token only exist
# after its JavaScript has run.  A real browser runs it, so the app never has
# to ask anybody to open DevTools.

#: The CDN's full-quality render of a page (the suffix the viewer itself asks
#: for).  Without it the same URL still works but is more heavily compressed.
DB_IMAGE_SUFFIX = "_lll.webp"
DB_COOKIE_SEED_DOMAINS = (".divyabhaskar.co.in", ".bhaskarassets.com",
                          ".bhaskar.com")

#: The `pt` access token of the current run (set by the browser discovery).
_db_runtime_pt: Optional[str] = None


def db_set_runtime_pt(token: str) -> None:
    global _db_runtime_pt
    _db_runtime_pt = token or None


def _db_cookie_pairs(cookie_header: str) -> List[Dict[str, object]]:
    """A 'k=v; k2=v2' header as Playwright cookie dicts for every DB host."""
    cookies: List[Dict[str, object]] = []
    for pair in cookie_header.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        for domain in DB_COOKIE_SEED_DOMAINS:
            cookies.append({"name": name.strip(), "value": value.strip(),
                            "domain": domain, "path": "/", "secure": True})
    return cookies


def _db_next_data(html: str) -> Optional[dict]:
    match = _DB_NEXT_DATA_RE.search(html or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def _db_epaper_detail(data: Optional[dict]) -> Dict[str, object]:
    """The epaperDetail block, wherever the app currently keeps it.

    The documented path is props.initialState.common.epaperDetail; the search
    fallback means a reshuffled state tree does not break extraction."""
    if not isinstance(data, dict):
        return {}
    node = data
    for key in ("props", "initialState", "common", "epaperDetail"):
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            break
    if isinstance(node, dict) and node.get("pgs"):
        return node
    found: List[Dict[str, object]] = []
    _db_find_detail(data, found)
    return found[0] if found else {}


def _db_find_detail(node, found: List[Dict[str, object]]) -> None:
    if found:
        return
    if isinstance(node, dict):
        if isinstance(node.get("pgs"), list) and node["pgs"]:
            found.append(node)
            return
        for value in node.values():
            _db_find_detail(value, found)
    elif isinstance(node, list):
        for item in node:
            _db_find_detail(item, found)


def _db_pt_from_detail(detail: Dict[str, object]) -> str:
    subs = detail.get("ss") if isinstance(detail, dict) else None
    if isinstance(subs, dict):
        data = subs.get("subsData")
        if isinstance(data, dict) and data.get("pt"):
            return str(data["pt"])
    return ""


def _db_pages_from_json(detail: Dict[str, object],
                        detail_url: str) -> List[PageRef]:
    """PageRefs from epaperDetail.pgs - the authoritative, ordered page list.

    imgUH is a /thumb/0x0/ URL: 0x0 means "as printed" (3600x2244 px), so it
    is used untouched - the /thumb/1600x0/ rewrite used elsewhere would make
    these pages SMALLER."""
    entries = detail.get("pgs") if isinstance(detail, dict) else None
    if not isinstance(entries, list):
        return []
    pages: List[PageRef] = []
    seen: set = set()
    for entry in entries[:DB_MAX_PAGES]:
        if not isinstance(entry, dict):
            continue
        image = entry.get("imgUH") or entry.get("imgH")
        if not isinstance(image, str) or not image.startswith("http"):
            continue
        if image in seen:
            continue
        seen.add(image)
        pages.append(PageRef(
            page_number=len(pages) + 1,
            image_url=image + DB_IMAGE_SUFFIX,
            thumb_url=image,
            page_html_url=detail_url))
    return pages


def _db_seed_cookie(reporter: ProgressReporter) -> str:
    """A login to hand a browser profile that has none: the live browser
    cookie store first, then the cookie saved by an earlier run.

    Quiet on purpose - the old code's "no logged-in browser session found,
    use Open PDF" warning is wrong here, because the very next line usually
    succeeds with the saved cookie."""
    cookie = db_import_browser_cookie(_SilentReporter())
    if cookie:
        reporter.log("  Seeded the session from the browser's own login.",
                     "dim")
        return cookie
    cookie = db_load_session_cookie(force=True)
    if cookie:
        reporter.log("  Seeded the session from the stored login.", "dim")
    return cookie


def _db_signed_in(html: str) -> bool:
    detail = _db_epaper_detail(_db_next_data(html))
    return bool(detail.get("pgs"))


def db_browser_discover(url: str, edition: str, date_str: str,
                        reporter: ProgressReporter
                        ) -> Tuple[List[PageRef], str, str]:
    """Open the detail page in a headless browser and read the page list.

    Returns (pages, cookie header, pt token) - all three empty when the
    browser is unavailable or the session is not logged in."""
    if not config.BROWSER_ENABLED:
        return [], "", ""
    detail_url = url if "/detail-page/" in url.lower() else (
        _db_detail_page_url(edition, date_str) or url)
    session = BrowserSession(reporter.log)
    try:
        session.start()
        # A profile that has never been used carries no login; seed it once
        # from whatever this machine already has.
        seed = ""
        if not _db_cookie_is_logged_in(session.cookie_header()):
            seed = _db_seed_cookie(reporter)
            if seed:
                try:
                    session.add_cookies(_db_cookie_pairs(seed))
                except Exception:
                    pass

        reporter.log("Opening the e-paper in a headless browser "
                     "(automatic session)...")
        capture = session.open(detail_url)
        detail = _db_epaper_detail(_db_next_data(capture.html))
        pages = _db_pages_from_json(detail, detail_url)

        if not pages and config.BROWSER_ALLOW_INTERACTIVE_LOGIN:
            # No login anywhere on this machine: ask for it ONCE, in a normal
            # sign-in window, then carry on headlessly forever.
            reporter.log("No Divya Bhaskar login found on this machine.",
                         "warn")
            if session.sign_in(detail_url, _db_cookie_is_logged_in):
                capture = session.open(detail_url)
                detail = _db_epaper_detail(_db_next_data(capture.html))
                pages = _db_pages_from_json(detail, detail_url)

        token = _db_pt_from_detail(detail)
        cookie = capture.cookies or session.cookie_header()
        if pages:
            reporter.log(f"  Browser session OK - {len(pages)} pages, "
                         f"access token {'yes' if token else 'no'}.",
                         "success")
            # The browser refreshed the tokens: keep them for the fallback
            # path and for the next run.
            if cookie and _db_cookie_is_logged_in(cookie):
                try:
                    db_save_session_cookie(cookie)
                except OSError:
                    pass
        elif capture.status and capture.status >= 400:
            reporter.log(f"  The site answered HTTP {capture.status}.", "warn")
        return pages, cookie, token
    except BrowserUnavailable as exc:
        reporter.log(f"  Browser automation unavailable: {exc}", "warn")
        return [], "", ""
    except ExtractionCancelled:
        raise
    except Exception as exc:
        reporter.log(f"  Browser automation failed: {exc}", "warn")
        return [], "", ""
    finally:
        session.close()


def db_refresh_access(page: PageRef, reporter: ProgressReporter) -> bool:
    """Mint a new `pt` token mid-run.

    The token is short-lived; a long edition can outlive the one issued at
    discovery.  Re-opening the detail page is all it takes - still no user
    involvement."""
    detail_url = page.page_html_url or ""
    if "/detail-page/" not in detail_url.lower() or not config.BROWSER_ENABLED:
        return False
    reporter.log("  Access token expired - refreshing the session...", "dim")
    session = BrowserSession(reporter.log)
    try:
        capture = session.open(detail_url)
        detail = _db_epaper_detail(_db_next_data(capture.html))
        token = _db_pt_from_detail(detail)
        if token:
            db_set_runtime_pt(token)
        if capture.cookies:
            db_set_runtime_cookie(capture.cookies)
        return bool(token or capture.cookies)
    except Exception:
        return False
    finally:
        session.close()

DB_URL_PATTERNS: Tuple["re.Pattern", ...] = (
    # The viewer's real route (confirmed):
    # https://www.divyabhaskar.co.in/epaper/detail-page/ahmedabad/12/2026-08-08
    re.compile(r"^https?://(?:www\.)?divyabhaskar\.co\.in/epaper/"
               r"detail-page/(?P<edition>[a-z0-9\-]+)"
               r"(?:/(?P<eid>\d+))?(?:/(?P<pdate>\d{4}-\d{2}-\d{2}))?"
               r"(?:[/?#].*)?$", re.IGNORECASE),
    re.compile(r"^https?://(?:www\.)?divyabhaskar\.co\.in/epaper"
               r"(?:/(?P<edition>[a-z0-9\-]+))?(?:[/?#].*)?$",
               re.IGNORECASE),
    re.compile(r"^https?://epaper\.divyabhaskar\.co\.in"
               r"(?:/(?P<edition>[a-z0-9\-]+))?(?:[/?#].*)?$",
               re.IGNORECASE),
    # Share short-links from the app / site (they redirect to the viewer):
    # https://divya.bhaskar.com/kbUzWHKYp5b
    re.compile(r"^https?://divya\.bhaskar\.com/[A-Za-z0-9]+/?"
               r"(?:[?#].*)?$", re.IGNORECASE),
)

# Edition -> numeric edCode for the detail-page route
# (/epaper/detail-page/<edition>/<edCode>/<date>).  Read from the live
# editions list; paste a browser URL once for any other city.
DB_EDITION_IDS: Dict[str, str] = {
    "ahmedabad": "12",
    "gandhinagar": "25",
    "surat": "38",
    "rajkot": "62",
    "vadodara": "32",
    "baroda": "32",
    "bhavnagar": "71",
    "bhuj": "23",
    "mehsana": "21",
    "jamnagar": "78",
    "junagadh": "77",
}

DB_IMAGE_HOST_HINTS: Tuple[str, ...] = (
    "bhaskarassets", "dainikbhaskar", "divyabhaskar", "bhaskar")
DB_IMAGE_BLACKLIST: Tuple[str, ...] = (
    "logo", "icon", "favicon", "sprite", "banner", "widget", "avatar",
    "advert", "promo", "login", "signup", "subscribe", "web-frontend",
    "placeholder")
DB_IMAGE_URL_RE = re.compile(
    r"https?:(?:\\/\\/|//)[^\s\"'<>\\{}]+?\.(?:jpg|jpeg|png)"
    r"(?:\?[^\s\"'<>\\{}]*)?", re.IGNORECASE)
DB_PDF_URL_RE = re.compile(
    r"https?:(?:\\/\\/|//)[^\s\"'<>\\{}]+?\.pdf"
    r"(?:\?[^\s\"'<>\\{}]*)?", re.IGNORECASE)
_DB_DETAIL_URL_RE = re.compile(
    r"https?://(?:www\.)?divyabhaskar\.co\.in/epaper/detail-page/"
    r"[^\s\"'<>\\]+", re.IGNORECASE)
_DB_NEXT_DATA_RE = re.compile(
    r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL)
_DB_HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)

_DB_IMAGE_KEYS = ("image", "imageurl", "img", "imgurl", "pageimage",
                  "photo", "url", "hdimage", "bigimage", "highres")
_DB_PAGENO_KEYS = ("pageno", "pagenumber", "page", "pgno", "srno",
                   "sequence", "order", "sortorder")

# Deep-discovery tuning: the viewer is a client-rendered app, so when its
# HTML carries no images the app's own data sources are chased.
_DB_BUILD_ID_RE = re.compile(r'"buildId"\s*:\s*"([A-Za-z0-9_\-]+)"')
_DB_SCRIPT_SRC_RE = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']",
                               re.IGNORECASE)
_DB_API_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\{}]+?(?:api|feed|epaper)[^\s\"'<>\\{}]*",
    re.IGNORECASE)
DB_BUNDLE_SCAN_LIMIT = 3           # JS bundles fetched per page
DB_API_PROBE_LIMIT = 8             # bounded API endpoint probes


def db_parse_url(url: str) -> Tuple[Optional[str], str]:
    """Validate a Divya Bhaskar URL; returns (edition or None, date)."""
    url = url.strip()
    for pattern in DB_URL_PATTERNS:
        match = pattern.match(url)
        if match:
            groups = match.groupdict()
            edition = (groups.get("edition") or "").lower() or None
            if edition == "detail-page":      # caught by a loose pattern
                edition = None
            if not edition:
                q = re.search(r"[?&]edition=([a-z0-9\-]+)", url,
                              re.IGNORECASE)
                edition = q.group(1).lower() if q else None
            date_str = groups.get("pdate") or None
            if not date_str:
                date_match = re.search(
                    r"(?:[?&]date=|/)(\d{4}-\d{2}-\d{2})(?:[/?#]|$)", url)
                date_str = date_match.group(1) if date_match else None
            return edition, date_str or date.today().isoformat()
    raise ExtractionError(
        "The URL is not a recognized Divya Bhaskar e-paper URL.\nExpected: "
        "https://www.divyabhaskar.co.in/epaper/detail-page/"
        "<edition>/<id>/YYYY-MM-DD")


def _db_unescape(url: str) -> str:
    return url.replace("\\/", "/")


def _db_normalize_for_scan(text: str) -> str:
    """Un-escape JSON / JS string forms so the URL regexes can see them."""
    return (text.replace("\\u002F", "/").replace("\\u002f", "/")
                .replace("\\/", "/"))


def _db_upscale_url(url: str) -> str:
    """Ask the CDN for a print-quality render: /thumb/444x0/ previews are
    rewritten to /thumb/1600x0/ and ?w= width params are raised."""
    url = re.sub(r"/thumb/\d+x\d+/", "/thumb/1600x0/", url)
    return re.sub(r"([?&](?:w|width)=)\d+", r"\g<1>1600", url)


def _db_absolutize(url: str, base_url: str) -> str:
    url = _db_unescape(url.strip())
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return urllib.parse.urljoin(base_url, url)
    return url


def _db_is_page_image(url: str) -> bool:
    """Heuristic: does this image URL look like an e-paper page render?"""
    low = url.lower()
    path = low.split("?", 1)[0]
    if not path.endswith((".jpg", ".jpeg", ".png")):
        return False
    if any(bad in low for bad in DB_IMAGE_BLACKLIST):
        return False
    thumb = re.search(r"/thumb/(\d+)x", low)
    if thumb and int(thumb.group(1)) < 800:
        return False          # tiny site thumbnail, not a page render
    try:
        netloc = urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return False
    if not any(hint in netloc for hint in DB_IMAGE_HOST_HINTS):
        return False
    return "epaper" in low


def _db_walk_json(node, found: List[Tuple[Optional[int], str]]) -> None:
    """Recursively collect (page_number, image_url) pairs from the app's
    embedded JSON state."""
    if isinstance(node, dict):
        page_no: Optional[int] = None
        for key in _DB_PAGENO_KEYS:
            for actual_key, value in node.items():
                if actual_key.lower() == key:
                    if isinstance(value, int):
                        page_no = value
                    elif isinstance(value, str) and value.isdigit():
                        page_no = int(value)
                    break
            if page_no is not None:
                break
        for actual_key, value in node.items():
            if isinstance(value, str) and \
                    actual_key.lower() in _DB_IMAGE_KEYS and \
                    value.split("?", 1)[0].lower().endswith(
                        (".jpg", ".jpeg", ".png")):
                found.append((page_no, value))
            else:
                _db_walk_json(value, found)
    elif isinstance(node, list):
        for item in node:
            _db_walk_json(item, found)


def db_extract_page_images(html: str, base_url: str) -> List[str]:
    """All plausible e-paper page-image URLs, best source first."""
    urls: List[str] = []

    # Strategy 1: structured page list inside __NEXT_DATA__.
    match = _DB_NEXT_DATA_RE.search(html)
    if match:
        structured: List[Tuple[Optional[int], str]] = []
        try:
            _db_walk_json(json.loads(match.group(1)), structured)
        except ValueError:
            structured = []
        numbered = [t for t in structured if t[0] is not None]
        chosen = sorted(numbered, key=lambda t: t[0]) \
            if len(numbered) >= 4 else structured
        urls.extend(u for _n, u in chosen)

    # Strategy 2: every e-paper-looking image URL in the raw HTML / inline
    # JSON (including \/ and / escaped forms).
    normalized = _db_normalize_for_scan(html)
    urls.extend(m.group(0) for m in DB_IMAGE_URL_RE.finditer(normalized))

    filtered: List[str] = []
    seen: set = set()
    for url in urls:
        absolute = _db_upscale_url(_db_absolutize(url, base_url))
        if not _db_is_page_image(absolute):
            continue
        key = absolute.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        filtered.append(absolute)

    # Strategy 3: relaxed fallback - a full page set shows up as a uniform
    # URL family (same host + directory) even without "epaper" in the path.
    if len(filtered) < 2:
        family = _db_relaxed_page_group(normalized)
        if len(family) > len(filtered):
            filtered = family
    return filtered


def _db_relaxed_page_group(normalized_html: str) -> List[str]:
    """Largest same-directory image family (>= 4 members) on a Bhaskar
    host - the shape of a newspaper page set - path wording ignored."""
    groups: Dict[str, List[str]] = {}
    group_seen: Dict[str, set] = {}
    for match in DB_IMAGE_URL_RE.finditer(normalized_html):
        url = _db_upscale_url(match.group(0))
        low = url.lower()
        if any(bad in low for bad in DB_IMAGE_BLACKLIST):
            continue
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            continue
        if not any(hint in parts.netloc.lower()
                   for hint in DB_IMAGE_HOST_HINTS):
            continue
        key = parts.netloc + parts.path.rsplit("/", 1)[0]
        path_key = url.split("?", 1)[0]
        bucket = group_seen.setdefault(key, set())
        if path_key in bucket:
            continue
        bucket.add(path_key)
        groups.setdefault(key, []).append(url)
    best: List[str] = []
    for family in groups.values():
        if len(family) > len(best):
            best = family
    return best if len(best) >= 4 else []


def _db_find_edition_link(html: str, edition: str,
                          base_url: str) -> Optional[str]:
    """Best 'open this edition' link on a viewer / landing page."""
    fallback: Optional[str] = None
    for match in _DB_HREF_RE.finditer(html):
        href = _db_absolutize(match.group(1), base_url)
        low = href.lower()
        if "epaper" not in low:
            continue
        if any(bad in low for bad in ("login", "signup", "signin",
                                      "subscribe", "register")):
            continue
        if low.rstrip("/") == base_url.lower().rstrip("/"):
            continue
        if "bhaskar" not in low:
            continue
        if edition and edition in low:
            return href
        if fallback is None:
            fallback = href
    return fallback


def _db_images_from_json_text(text: str, page_url: str) -> List[str]:
    """Page images from an API/JSON payload; strict filter first, then the
    uniform-family fallback for CDNs without 'epaper' in the path."""
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    found: List[Tuple[Optional[int], str]] = []
    _db_walk_json(payload, found)
    numbered = [t for t in found if t[0] is not None]
    chosen = sorted(numbered, key=lambda t: t[0]) \
        if len(numbered) >= 4 else found
    strict: List[str] = []
    relaxed: List[str] = []
    seen: set = set()
    for _n, raw in chosen:
        absolute = _db_absolutize(raw, page_url)
        key = absolute.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        low = absolute.lower()
        if any(bad in low for bad in DB_IMAGE_BLACKLIST):
            continue
        try:
            netloc = urllib.parse.urlsplit(absolute).netloc.lower()
        except ValueError:
            continue
        if not any(hint in netloc for hint in DB_IMAGE_HOST_HINTS):
            continue
        relaxed.append(absolute)
        if _db_is_page_image(absolute):
            strict.append(absolute)
    if strict:
        return strict
    return relaxed if len(relaxed) >= 4 else []


def _db_probe_dynamic(downloader: PageDownloader, html: str, page_url: str,
                      edition: str, date_str: str,
                      reporter: ProgressReporter) -> List[str]:
    """Chase a client-rendered page's data sources for its page images:
    1. the Next.js /_next/data/<buildId>/<route>.json of this route,
    2. same-site JS bundles (scanned for image families and API URLs),
    3. a bounded set of probes of discovered e-paper API endpoints."""
    split = urllib.parse.urlsplit(page_url)
    origin = f"{split.scheme}://{split.netloc}"

    # -- 1. Next.js data route ------------------------------------------------
    build = _DB_BUILD_ID_RE.search(html)
    if build:
        route = split.path.rstrip("/") or "/index"
        for data_path in dict.fromkeys((route, "/epaper")):
            reporter.check_cancel()
            data_url = (f"{origin}/_next/data/{build.group(1)}"
                        f"{data_path}.json")
            try:
                text = downloader.fetch_text(
                    data_url, referer=page_url,
                    extra_headers=_db_headers())
            except ExtractionError:
                continue
            images = _db_images_from_json_text(text, page_url)
            if images:
                reporter.log("  Found pages via the app's data route.",
                             "dim")
                return images

    # -- 2. JS bundles ----------------------------------------------------------
    script_srcs: List[str] = []
    for match in _DB_SCRIPT_SRC_RE.finditer(html):
        src = _db_absolutize(match.group(1), page_url)
        low = src.lower()
        if not low.split("?", 1)[0].endswith(".js"):
            continue
        if "bhaskar" not in low and not src.startswith(origin):
            continue
        if src not in script_srcs:
            script_srcs.append(src)
    bundle_images: List[str] = []
    api_urls: List[str] = []
    for src in script_srcs[:DB_BUNDLE_SCAN_LIMIT]:
        reporter.check_cancel()
        try:
            text = downloader.fetch_text(src, referer=page_url,
                                         extra_headers=_db_headers())
        except ExtractionError:
            continue
        normalized = _db_normalize_for_scan(text)
        for m in DB_IMAGE_URL_RE.finditer(normalized):
            absolute = _db_absolutize(m.group(0), page_url)
            if _db_is_page_image(absolute) and absolute not in bundle_images:
                bundle_images.append(absolute)
        for m in _DB_API_URL_RE.finditer(normalized):
            candidate = m.group(0).rstrip("\\/")
            low = candidate.lower()
            if "epaper" in low and candidate not in api_urls and \
                    not low.endswith((".js", ".css", ".png", ".jpg",
                                      ".jpeg", ".svg", ".woff", ".woff2")):
                api_urls.append(candidate)
    if len(bundle_images) >= 2:
        reporter.log("  Found pages inside the app's scripts.", "dim")
        return bundle_images

    # -- 3. API probes ----------------------------------------------------------
    probes: List[str] = []
    for endpoint in api_urls:
        separator = "&" if "?" in endpoint else "?"
        probes.append(endpoint)
        probes.append(f"{endpoint}{separator}edition={edition}"
                      f"&date={date_str}")
        probes.append(f"{endpoint}{separator}slug={edition}"
                      f"&date={date_str}")
    for probe in probes[:DB_API_PROBE_LIMIT]:
        reporter.check_cancel()
        try:
            text = downloader.fetch_text(probe, referer=page_url,
                                         extra_headers=_db_headers())
        except ExtractionError:
            continue
        images = _db_images_from_json_text(text, page_url)
        if images:
            reporter.log("  Found pages via an app API endpoint.", "dim")
            return images
    return []


def _db_write_debug(parts: List[Tuple[str, str]]) -> Optional[str]:
    """Dump everything fetched into data/debug so the extractor can be
    matched to the live site layout offline."""
    try:
        path = os.path.join(config.debug_dir("divya_bhaskar"),
                            "divyabhaskar_debug.html")
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write("<!-- Public Notice Extractor: Divya Bhaskar "
                     "diagnostic dump -->\n")
            for source, text in parts:
                fh.write(f"\n\n<!-- ========== SOURCE: {source} "
                         f"========== -->\n")
                fh.write(text[:200000])
        return path
    except Exception:
        return None


DB_CDN_PAGE_BASE = ("https://images.bhaskarassets.com/thumb/1600x0/"
                    "epaper/gujrat/epaperimages")
# edition slug -> (CDN code, hyphenated "pg-N" naming?)  (from live URLs)
DB_EDITION_CODES: Dict[str, Tuple[str, bool]] = {
    "ahmedabad":   ("ahmedabad_city", True),
    "gandhinagar": ("gandhinagar_pullout", False),
    "baroda":      ("baroda_city", True),
    "surat":       ("surat_city", True),
    "rajkot":      ("rajkot_city", True),
    "bhavnagar":   ("bhavnagar_dak", True),
    "bhuj":        ("bhujdaak", True),
}


def _db_weekday_prefix(date_str: str) -> str:
    """CDN filename prefix: weekday number, Sunday=1 .. Saturday=7."""
    day = date.fromisoformat(date_str)
    return str((day.weekday() + 1) % 7 + 1)


def _db_page_url(edition: str, date_str: str, page_no: int) -> str:
    """Direct CDN URL of one page of one edition on one date."""
    code, hyphenated = DB_EDITION_CODES.get(
        edition, (f"{edition}_city", True))
    day = date.fromisoformat(date_str)
    folder = f"{day.day:02d}{day.month:02d}{day.year}"
    page_part = f"pg-{page_no}" if hyphenated else f"pg{page_no}"
    prefix = _db_weekday_prefix(date_str)
    return f"{DB_CDN_PAGE_BASE}/{folder}/{prefix}{code}-{page_part}-0.jpg"


def _db_cdn_probe(downloader: PageDownloader, edition: str, date_str: str,
                  reporter: ProgressReporter) -> List[PageRef]:
    """Fetch pages straight from the Bhaskar image CDN by pattern.
    Returns as many consecutive pages as the CDN will serve: page 1 is
    public, the rest need the stored premium session cookie."""
    pages: List[PageRef] = []
    misses = 0
    denied = 0
    reporter.log("Trying the direct CDN page pattern...")
    for page_no in range(1, DB_MAX_PAGES + 1):
        reporter.check_cancel()
        page_url = _db_page_url(edition, date_str, page_no)
        try:
            image = downloader.fetch_image(
                page_url, referer="https://www.divyabhaskar.co.in/",
                extra_headers=_db_headers())
        except ExtractionError as exc:
            misses += 1
            if "403" in str(exc):
                denied += 1
            if misses >= 2 and page_no >= 3:
                break
            continue
        height, width = image.shape[:2]
        if height <= width or width < 400:
            misses += 1
            continue
        misses = 0
        pages.append(PageRef(
            page_number=page_no, image_url=page_url, thumb_url=page_url,
            page_html_url="https://www.divyabhaskar.co.in/epaper"))
    if pages and len(pages) < 3 and denied:
        reporter.log("  The CDN refused the inner pages (premium lock).",
                     "warn")
    return pages


def _db_plausible_edition(images: List[str],
                          reporter: ProgressReporter) -> List[str]:
    """A real edition has many pages; one or two stray images are almost
    always site furniture (login banners etc.), never the paper."""
    if 0 < len(images) < 3:
        reporter.log(f"  Ignoring {len(images)} stray image(s) - not a "
                     "full edition.", "dim")
        return []
    return images


def _db_detail_page_url(edition: str, date_str: str) -> Optional[str]:
    """Build the detail-page URL for an edition+date, or None when the
    edCode is unknown."""
    ed_code = DB_EDITION_IDS.get(edition)
    if not ed_code:
        return None
    return ("https://www.divyabhaskar.co.in/epaper/detail-page/"
            f"{edition}/{ed_code}/{date_str}")


def _db_pages_from_detail(downloader: PageDownloader, detail_url: str,
                          reporter: ProgressReporter) -> List[PageRef]:
    """Plain-HTTP version of the browser discovery: fetch the detail page with
    whatever session this machine has and read the same page list out of its
    __NEXT_DATA__.  Empty when the response is the logged-out shell.

    Server-side rendering is what makes this work at all without a browser -
    but only a logged-in request gets the pgs list, which is why the browser
    path above is tried first."""
    try:
        html = downloader.fetch_text(
            detail_url, referer="https://www.divyabhaskar.co.in/",
            extra_headers=_db_headers())
    except ExtractionError as exc:
        reporter.log(f"  Could not open the detail page: {exc}", "warn")
        return []
    detail = _db_epaper_detail(_db_next_data(html))
    token = _db_pt_from_detail(detail)
    if token:
        db_set_runtime_pt(token)
    return _db_pages_from_json(detail, detail_url)


def db_discover_pages(downloader: PageDownloader, url: str,
                      reporter: ProgressReporter) -> List[PageRef]:
    edition, date_str = db_parse_url(url)
    edition = edition or "ahmedabad"
    db_set_runtime_cookie("")            # reset; the browser sets it below
    db_set_runtime_pt("")
    reporter.log(f"Opening edition: {edition} / {date_str}")

    # PRIMARY: drive a real (headless) browser.  It runs the viewer's
    # JavaScript, so it gets the session cookies and the access token by
    # itself - the whole reason nothing has to be pasted in by hand.
    pages, browser_cookie, token = db_browser_discover(
        url, edition, date_str, reporter)
    if browser_cookie:
        db_set_runtime_cookie(browser_cookie)
        downloader.seed_cookies(browser_cookie, DB_COOKIE_SEED_DOMAINS)
    if token:
        db_set_runtime_pt(token)
    if pages:
        reporter.log(f"Edition has {len(pages)} pages.")
        return pages
    reporter.log("Browser discovery found no pages - falling back to the "
                 "direct routes.", "warn")

    # FALLBACK (no Playwright / no login): the cookie stores this machine
    # already has.
    cookie = browser_cookie or db_import_browser_cookie(reporter)
    if cookie:
        db_set_runtime_cookie(cookie)     # used by _db_headers this run
    else:
        cookie = db_load_session_cookie(force=True)
        if cookie:
            reporter.log("Using the stored Divya Bhaskar login session.",
                         "info")
    if cookie:
        downloader.seed_cookies(cookie, (
            ".divyabhaskar.co.in", ".bhaskarassets.com", ".bhaskar.com"))
    else:
        reporter.log("No login session.  Enable Tools > Divya Bhaskar "
                     "Auto-Login (reads your browser) once, or paste a "
                     "cookie - otherwise only the cover page is visible.",
                     "dim")
    if date_str != date.today().isoformat():
        reporter.log("  NOTE: the Divya Bhaskar web viewer usually serves "
                     "only recent days; the site may ignore the chosen "
                     "date.", "warn")
    # The e-paper is natively distributed as a PDF: a direct .pdf link
    # (pasted straight from the browser) skips the page hunt entirely.
    if url.split("?", 1)[0].lower().endswith(".pdf"):
        return pdf_pages_from_web(downloader, url, reporter)

    # PRIMARY (verified live): the detail page embeds the full ordered page
    # list in __NEXT_DATA__ (epaperDetail.pgs) once the premium session
    # cookie is attached.  Each page is a WebP; /thumb/0x0/ is upscaled to
    # /thumb/1600x0/ for full print resolution.
    detail_url = url if "/detail-page/" in url.lower() \
        else (_db_detail_page_url(edition, date_str) or url)
    detail_pages = _db_pages_from_detail(downloader, detail_url, reporter)
    if len(detail_pages) >= 2:
        reporter.log(f"Edition has {len(detail_pages)} pages.")
        return detail_pages
    if not db_load_session_cookie():
        raise ExtractionError(
            "Divya Bhaskar needs a login to list the pages, and none could "
            "be obtained automatically.\n\n"
            "The app drives a headless browser for this, so the usual fix "
            "is to let it install: press 'Download Dependencies' once "
            "(it adds Playwright + Chromium), then Extract again - a "
            "sign-in window appears once and is remembered.\n\n"
            "Offline alternative: download the day's PDF in your browser "
            "and press 'Open PDF...'.")

    # The site's routes are unstable (the /epaper/<edition> form 404s), so
    # several entry points are tried: the given URL first, then the viewer
    # roots on both domains.  For every page that loads but carries no
    # images (client-rendered shell), the app's own data sources are chased
    # too - Next.js data route, JS bundles and API endpoints.
    candidates: List[str] = [url]
    for root in ("https://www.divyabhaskar.co.in/epaper",
                 "https://epaper.divyabhaskar.co.in/"):
        if all(root.rstrip("/") != c.rstrip("/") for c in candidates):
            candidates.append(root)

    images: List[str] = []
    source_url = url
    debug_parts: List[Tuple[str, str]] = []
    candidate_index = 0
    while candidate_index < len(candidates):
        candidate = candidates[candidate_index]
        candidate_index += 1
        reporter.check_cancel()
        try:
            html = downloader.fetch_text(
                candidate, referer="https://www.divyabhaskar.co.in/",
                extra_headers=_db_headers())
        except ExtractionError as exc:
            reporter.log(f"  Could not open {candidate} - trying the next "
                         "entry point...", "warn")
            debug_parts.append((candidate, f"(fetch failed: {exc})"))
            continue
        debug_parts.append((candidate, html))
        source_url = candidate
        # Share links / viewer shells often reference the real detail
        # page - queue those as extra entry points.
        for extra_url in _DB_DETAIL_URL_RE.findall(
                _db_normalize_for_scan(html))[:3]:
            if extra_url not in candidates:
                reporter.log(f"  Queued viewer link: {extra_url}", "dim")
                candidates.append(extra_url)
        # PDF links first - the e-paper is natively a PDF.
        page_norm = _db_normalize_for_scan(html)
        pdf_links: List[str] = []
        for pdf_match in DB_PDF_URL_RE.finditer(page_norm):
            absolute = _db_absolutize(pdf_match.group(0), candidate)
            if "bhaskar" in absolute.lower() and absolute not in pdf_links:
                pdf_links.append(absolute)
        for pdf_link in pdf_links[:2]:
            reporter.log(f"  Found a PDF link: {pdf_link}", "dim")
            reporter.check_cancel()
            try:
                return pdf_pages_from_web(downloader, pdf_link, reporter)
            except ExtractionError as exc:
                reporter.log(f"  PDF link failed: {exc}", "warn")
        images = _db_plausible_edition(
            db_extract_page_images(html, candidate), reporter)
        if images:
            break
        # The page is an empty client-rendered shell - chase its data.
        images = _db_plausible_edition(
            _db_probe_dynamic(downloader, html, candidate,
                              edition, date_str, reporter), reporter)
        if images:
            break
        # Still nothing - follow the best edition link once.
        link = _db_find_edition_link(html, edition, candidate)
        if link:
            reporter.log(f"  Following edition link: {link}", "dim")
            reporter.check_cancel()
            try:
                html = downloader.fetch_text(
                    link, referer=candidate,
                    extra_headers=_db_headers())
            except ExtractionError:
                continue
            debug_parts.append((link, html))
            source_url = link
            images = _db_plausible_edition(
                db_extract_page_images(html, link), reporter)
            if not images:
                images = _db_plausible_edition(
                    _db_probe_dynamic(downloader, html, link,
                                      edition, date_str, reporter),
                    reporter)
            if images:
                break

    if not images:
        # A cookie is saved but the detail page still had no list - session
        # likely expired.
        if db_load_session_cookie():
            reporter.log("The stored login session did not return a page "
                         "list - it may have expired.  Re-save it via "
                         "Tools > Divya Bhaskar Login...", "warn")
        debug_path = _db_write_debug(debug_parts)
        hint = ""
        if debug_path:
            reporter.log(f"Diagnostic file saved: {debug_path}", "warn")
            hint = (f"\n\nA diagnostic file was saved to:\n  {debug_path}\n"
                    "Share that file and the extractor can be matched to "
                    "the site's current layout.")
        raise ExtractionError(
            "Could not find the Divya Bhaskar e-paper pages automatically."
            "\n\nNote: this program cannot use the browser's premium "
            "login session.  If the pages need your premium account, "
            "download the day's PDF in the browser and press "
            "'Open PDF...' (or paste the PDF's direct link)." + hint)

    dump_path = _db_write_debug(debug_parts)
    if dump_path:
        reporter.log(f"Page-source dump saved (for troubleshooting): "
                     f"{dump_path}", "dim")

    pages: List[PageRef] = []
    for index, image_url in enumerate(images[:DB_MAX_PAGES], start=1):
        pages.append(PageRef(
            page_number=index,
            image_url=_db_upscale_url(image_url),
            thumb_url=image_url,
            page_html_url=source_url,
        ))
    reporter.log(f"Edition has {len(pages)} pages.")
    return pages


#: Pages are fetched in parallel; one expiry must trigger ONE refresh, not
#: one per in-flight page.
_DB_REFRESH_LOCK = threading.Lock()


def db_get_page_image(downloader: PageDownloader, page: PageRef,
                      reporter: ProgressReporter) -> "np.ndarray":
    if page.image_url is None:        # page comes from a downloaded PDF
        return pdf_render_page(page.page_html_url, page.page_number)

    def fetch() -> "np.ndarray":
        return downloader.fetch_image(
            page.image_url, referer="https://www.divyabhaskar.co.in/",
            extra_headers=_db_headers())

    try:
        image = fetch()
    except ExtractionError as exc:
        if "401" not in str(exc) and "403" not in str(exc):
            raise
        # The access token is short-lived.  Mint a new one from the viewer
        # and try this page once more - a stale token is no longer a dead
        # end, and never involves the user.
        refreshed = False
        with _DB_REFRESH_LOCK:
            refreshed = db_refresh_access(page, reporter)
        if refreshed:
            try:
                image = fetch()
            except ExtractionError:
                refreshed = False
        if not refreshed:
            raise ExtractionError(
                "AUTH: Divya Bhaskar refused the reading pages (HTTP "
                "401/403) even after refreshing the session.\n\n"
                "The account may have no active e-paper subscription for "
                "this edition, or the site wants a fresh sign-in: run "
                "Tools > Divya Bhaskar Login (Session)... once, or "
                "download the day's PDF and press 'Open PDF...'.")
    height, width = image.shape[:2]
    if width < 450 or height <= width:
        # Newspaper pages are large and portrait; anything else is site
        # furniture (login banner, promo graphic, ...).
        raise ExtractionError(
            f"Not a newspaper page render ({width}x{height} px) - "
            "probably a site banner.  If the e-paper needs your premium "
            "login, save it via Tools > Divya Bhaskar Login... or use "
            "'Open PDF...'.")
    if width < 900:
        # Free/preview renders are small; double them so the notice
        # pill headers reach the template matching scale range.
        image = cv2.resize(image, (width * 2, height * 2),
                           interpolation=cv2.INTER_CUBIC)
    return image


class DivyaBhaskarPipeline(NoticeDetectionPipeline):
    """Divya Bhaskar: real white-on-black જાહેર નોટિસ pill headers cropped
    from an actual DB Ahmedabad page (db- samples), with the Sandesh and
    Gujarat Samachar crops as backup."""
    newspaper_name = "Divya Bhaskar"
    default_config = DB_DETECTION_CONFIG
    embedded_prefixes = ("db-", "sandesh-", "gs-", "chetavni-")


class DivyaBhaskarExtractor(BaseNewspaperExtractor):
    """Divya Bhaskar e-paper (www.divyabhaskar.co.in/epaper).

    Fully automatic: a headless browser opens the edition's detail page,
    which yields the page list and the CDN access token (section 6c-1).  The
    heuristic scrapers below it are the fallback for machines that cannot run
    a browser."""

    display_name = "Divya Bhaskar"
    days_back_limit = 7        # the web viewer effectively serves recent days
    pipeline_cls = DivyaBhaskarPipeline
    editions = ("ahmedabad", "gandhinagar", "baroda", "surat", "rajkot",
                "bhavnagar", "bhuj")
    # "Extract All" / "All editions" only loop the two monitored cities.
    loop_editions = ("ahmedabad", "gandhinagar")
    zero_results_hint = (
        "Divya Bhaskar: pages were downloaded but no જાહેર નોટિસ header "
        "matched.  The first pages and a score report were saved under "
        "notice_extractor/data/debug/divya_bhaskar - share page_01.png "
        "(or a screenshot of one notice) so detection can be tuned.")
    debug_on_zero = True

    @classmethod
    def matches(cls, url: str) -> bool:
        stripped = url.strip()
        return any(p.match(stripped) for p in DB_URL_PATTERNS)

    @classmethod
    def build_url(cls, edition: str, day: "date") -> str:
        # Real viewer route: /epaper/detail-page/<edition>/<id>/<date>.
        # Editions without a known id fall back to the viewer root.
        edition_id = DB_EDITION_IDS.get(edition)
        if edition_id:
            return ("https://www.divyabhaskar.co.in/epaper/detail-page/"
                    f"{edition}/{edition_id}/{day.isoformat()}")
        return (f"https://www.divyabhaskar.co.in/epaper"
                f"?edition={edition}&date={day.isoformat()}")

    @classmethod
    def edition_from_url(cls, url: str) -> Optional[str]:
        try:
            edition, _date_str = db_parse_url(url)
        except ExtractionError:
            return None
        return edition

    def discover(self, downloader, url, reporter):
        return db_discover_pages(downloader, url, reporter)

    def fetch_page(self, downloader, page, reporter):
        return db_get_page_image(downloader, page, reporter)


#: registry entry read by the package loader
NEWSPAPER = DivyaBhaskarExtractor
