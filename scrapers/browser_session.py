"""Headless-browser session - automatic cookies, no DevTools, no copy/paste.

Why a browser at all: divyabhaskar.co.in serves the e-paper from a
client-rendered Next.js app that mints its own session cookies in JavaScript
and refreshes them every few minutes.  urllib can fetch that page but never
gets the cookies, which is why the old route ended in "open DevTools, copy the
Cookie header".  A real browser does it by itself:

    with BrowserSession(log) as session:
        capture = session.open(detail_url)
        capture.html      # the RENDERED page (__NEXT_DATA__ is populated)
        capture.cookies   # "k=v; k2=v2", ready for PageDownloader.seed_cookies
        capture.images    # every page-image URL the viewer actually requested

The page images are NOT downloaded here.  The browser hands over its cookies
and the existing downloader pulls the pages in parallel over plain HTTP, which
is several times faster than driving a browser through 20 pages.

Session persistence: the context is a PERSISTENT profile under
`data/browser_profile`, so a login done once survives every later run - the
"one-time sign-in" really happens once, and every run after it is headless.

Playwright only.  Selenium would need its own driver management, its own
network-interception story (CDP) and a second code path to keep working; the
one automation stack that ships its browsers is enough.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .. import config

Logger = Callable[..., None]

#: Same string the plain-HTTP downloader sends, so the site sees one visitor.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class BrowserUnavailable(RuntimeError):
    """Playwright or its browser binary is missing / would not start."""


# Response URLs worth remembering: page renders and the JSON that lists them.
_IMAGE_URL_RE = re.compile(r"\.(?:webp|jpe?g|png)(?:\?|$)", re.IGNORECASE)
_JUNK_URL_RE = re.compile(
    r"(logo|icon|favicon|sprite|banner|widget|avatar|advert|promo|"
    r"placeholder|google|facebook|doubleclick|gtm\.|analytics)",
    re.IGNORECASE)


#: Chromium single-instances a profile directory: two agents opening the same
#: one at the same time is a lock fight over the cookie database, and the
#: loser's login silently disappears.  Sessions therefore take turns.  Only
#: discovery runs inside a session (a few seconds); the pages themselves are
#: downloaded outside it, in parallel, by the normal downloader.
_PROFILE_LOCK = threading.RLock()


def _noop(*_args, **_kwargs) -> None:
    pass


def default_browser_channel() -> str:
    """The system default browser as a Playwright channel name.

    Windows keeps the choice in the registry; Chrome and Edge can both be
    driven directly (same engine, the user's own install).  Anything else
    (Firefox, Brave, ...) falls back to Playwright's bundled Chromium, which
    is always present and behaves identically for this job."""
    if not sys.platform.startswith("win"):
        return ""
    try:
        import winreg
        key = (r"Software\Microsoft\Windows\Shell\Associations"
               r"\UrlAssociations\https\UserChoice")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            prog_id = str(winreg.QueryValueEx(handle, "ProgId")[0]).lower()
    except Exception:
        return ""
    if "chrome" in prog_id and "chromium" not in prog_id:
        return "chrome"
    if "msedge" in prog_id or "edge" in prog_id:
        return "msedge"
    return ""


@dataclass
class Capture:
    """What one page visit produced."""
    url: str
    html: str = ""
    cookies: str = ""
    status: int = 0
    #: every response URL seen, in network order
    responses: List[str] = field(default_factory=list)
    #: page-image candidates, de-duplicated, in network order
    images: List[str] = field(default_factory=list)
    #: JSON endpoints the viewer called (for diagnostics / new-layout hunting)
    json_urls: List[str] = field(default_factory=list)

    def has_pages(self, minimum: int = 2) -> bool:
        return len(self.images) >= minimum


class BrowserSession:
    """One Chromium profile, driven headlessly.

    Playwright's sync API is per-thread: build and close a session on the same
    thread (each extraction agent gets its own, which is exactly how the job
    runner already works).
    """

    def __init__(self, log: Optional[Logger] = None, *,
                 headless: Optional[bool] = None,
                 profile_dir: Optional[str] = None,
                 channel: Optional[str] = None,
                 block_images: bool = True) -> None:
        self._log = log or _noop
        self._headless = config.BROWSER_HEADLESS if headless is None \
            else headless
        self._profile_dir = profile_dir or config.BROWSER_PROFILE_DIR
        self._channel = config.BROWSER_CHANNEL if channel is None else channel
        self._block_images = block_images
        self._playwright = None
        self._context = None
        self._holds_lock = False

    # -- lifecycle ------------------------------------------------------------
    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def start(self) -> None:
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "Playwright is not installed - press 'Download Dependencies' "
                "in the app, or run:  pip install playwright  &&  "
                "python -m playwright install chromium") from exc

        _PROFILE_LOCK.acquire()
        self._holds_lock = True
        try:
            self._start_locked(sync_playwright)
        except BaseException:
            self._release_lock()
            raise

    def _start_locked(self, sync_playwright) -> None:
        self._playwright = sync_playwright().start()
        channel = self._channel or default_browser_channel()
        try:
            self._context = self._launch(channel)
        except BrowserUnavailable:
            raise
        except Exception as exc:
            # A missing bundled browser is the one failure worth fixing
            # automatically; everything else is reported as-is.
            if not _looks_like_missing_browser(exc):
                self._stop_playwright()
                raise BrowserUnavailable(f"Could not start the browser: {exc}")
            self._log("  Downloading the automation browser "
                      "(one-time, ~120 MB)...", "info")
            if not install_browser(self._log):
                self._stop_playwright()
                raise BrowserUnavailable(
                    "Could not install Playwright's Chromium.  Run this once "
                    "in a terminal:  python -m playwright install chromium")
            try:
                self._context = self._launch("")
            except Exception as exc2:
                self._stop_playwright()
                raise BrowserUnavailable(
                    f"Could not start the browser: {exc2}")

    def _launch(self, channel: str):
        """A persistent context, so cookies + login outlive the process."""
        config.migrate_browser_profile()      # from data/, if still there
        os.makedirs(self._profile_dir, exist_ok=True)
        options: Dict[str, object] = dict(
            user_data_dir=self._profile_dir,
            headless=self._headless,
            viewport={"width": 1440, "height": 1000},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            # Headless Chrome announces itself as "HeadlessChrome/..." and
            # Bhaskar's edge answers that with a flat 403.  A normal Chrome
            # UA is not a disguise here - it is the same engine, driven by
            # the same user, asking for the same page.
            user_agent=USER_AGENT,
            args=["--disable-blink-features=AutomationControlled",
                  "--disable-features=IsolateOrigins,site-per-process"],
        )
        if channel:
            options["channel"] = channel
        try:
            context = self._playwright.chromium.launch_persistent_context(
                **options)
        except Exception:
            if not channel:
                raise
            # The installed Chrome/Edge was not usable (running with a lock,
            # policy-managed, ...) - the bundled Chromium always is.
            self._log(f"  {channel} could not be driven; using the bundled "
                      "Chromium instead.", "dim")
            options.pop("channel", None)
            context = self._playwright.chromium.launch_persistent_context(
                **options)
        context.set_default_timeout(config.BROWSER_NAV_TIMEOUT_MS)
        self._log(f"  Browser session ready ({channel or 'chromium'}, "
                  f"{'headless' if self._headless else 'visible'}).", "dim")
        return context

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        self._stop_playwright()
        self._release_lock()

    def _release_lock(self) -> None:
        if self._holds_lock:
            self._holds_lock = False
            try:
                _PROFILE_LOCK.release()
            except RuntimeError:
                pass

    def _stop_playwright(self) -> None:
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    # -- work -----------------------------------------------------------------
    def open(self, url: str, *, wait_for: str = "",
             settle_ms: Optional[int] = None,
             image_filter: Optional[Callable[[str], bool]] = None) -> Capture:
        """Load `url` and return everything that visit produced.

        Network interception is the point: the viewer fetches its page list
        and its page renders over XHR, so watching the requests finds them
        even when the HTML never names them."""
        self.start()
        assert self._context is not None
        capture = Capture(url=url)
        page = self._context.new_page()
        keep = image_filter or _default_image_filter

        def on_request(request) -> None:
            target = request.url
            capture.responses.append(target)
            if _IMAGE_URL_RE.search(target) and keep(target):
                if target not in capture.images:
                    capture.images.append(target)

        def on_response(response) -> None:
            target = response.url
            content_type = (response.headers or {}).get("content-type", "")
            if "json" in content_type.lower() and \
                    target not in capture.json_urls:
                capture.json_urls.append(target)

        page.on("request", on_request)
        page.on("response", on_response)
        if self._block_images:
            # The page renders are downloaded later over plain HTTP with these
            # cookies - letting the browser pull them too doubles the traffic.
            page.route(
                re.compile(r"\.(?:webp|jpe?g|png|gif|mp4|woff2?)(?:\?|$)",
                           re.IGNORECASE),
                lambda route: route.abort())
        try:
            response = page.goto(url, wait_until="domcontentloaded",
                                 timeout=config.BROWSER_NAV_TIMEOUT_MS)
            capture.status = response.status if response is not None else 0
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=8_000)
                except Exception:
                    pass
            self._settle(page, settle_ms)
            capture.html = page.content()
        except Exception as exc:
            raise BrowserUnavailable(f"{url}: {exc}") from exc
        finally:
            try:
                page.close()
            except Exception:
                pass
        capture.cookies = self.cookie_header()
        return capture

    def _settle(self, page, settle_ms: Optional[int]) -> None:
        """Wait for the app's own XHRs.  networkidle is the right signal but
        ad/analytics sockets can keep a page 'busy' forever, so it is capped
        by a plain wait."""
        budget = config.BROWSER_SETTLE_MS if settle_ms is None else settle_ms
        try:
            page.wait_for_load_state("networkidle", timeout=budget)
        except Exception:
            page.wait_for_timeout(min(budget, 3_000))

    def add_cookies(self, cookies: Sequence[dict]) -> None:
        """Hand the profile a login it does not have yet (first run on a
        machine that is already signed in elsewhere)."""
        self.start()
        assert self._context is not None
        self._context.add_cookies(list(cookies))

    def cookie_header(self, domains: Sequence[str] = ()) -> str:
        """Current cookies as a 'k=v; k2=v2' header."""
        if self._context is None:
            return ""
        try:
            cookies = self._context.cookies()
        except Exception:
            return ""
        jar: Dict[str, str] = {}
        for cookie in cookies:
            host = str(cookie.get("domain", "")).lstrip(".").lower()
            if domains and not any(host == d.lstrip(".").lower()
                                   or host.endswith(
                                       "." + d.lstrip(".").lower())
                                   for d in domains):
                continue
            name, value = cookie.get("name"), cookie.get("value")
            if name and value:
                jar[str(name)] = str(value)
        return "; ".join(f"{k}={v}" for k, v in jar.items())

    def sign_in(self, url: str,
                is_signed_in: Callable[[str], bool],
                wait_seconds: Optional[int] = None) -> bool:
        """Open a VISIBLE window once so the user can log in, then keep the
        session in the profile forever.

        This is the only step a human is ever asked for, it happens at most
        once per machine, and it is a normal login page - never DevTools."""
        if not config.BROWSER_ALLOW_INTERACTIVE_LOGIN:
            return False
        budget = wait_seconds or config.BROWSER_LOGIN_WAIT_SECONDS
        self.close()
        self._headless = False
        self._block_images = False
        self._log("A sign-in window has opened - log in to Divya Bhaskar "
                  "there.  It is remembered, so this happens only once.",
                  "warn")
        self.start()
        assert self._context is not None
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded",
                      timeout=config.BROWSER_NAV_TIMEOUT_MS)
            deadline = time.monotonic() + budget
            while time.monotonic() < deadline:
                if is_signed_in(self.cookie_header()):
                    self._log("Signed in - the session is stored for every "
                              "later run.", "success")
                    return True
                try:
                    page.wait_for_timeout(2_000)
                except Exception:
                    break            # the user closed the window
            self._log("Sign-in window timed out.", "warn")
            return False
        except Exception as exc:
            self._log(f"Sign-in window failed: {exc}", "warn")
            return False
        finally:
            try:
                page.close()
            except Exception:
                pass
            self.close()
            self._headless = config.BROWSER_HEADLESS
            self._block_images = True


def _default_image_filter(url: str) -> bool:
    """Big enough to be a newspaper page, not site furniture."""
    if _JUNK_URL_RE.search(url):
        return False
    thumb = re.search(r"/thumb/(\d+)x", url.lower())
    if not thumb:
        return True
    # /thumb/0x0/ is this CDN's "as printed" size, not a zero-pixel image.
    width = int(thumb.group(1))
    return width == 0 or width >= 700


def _looks_like_missing_browser(exc: Exception) -> bool:
    text = str(exc).lower()
    return ("executable doesn't exist" in text
            or "please run the following command" in text
            or "playwright install" in text)


def install_browser(log: Optional[Logger] = None) -> bool:
    """Download Playwright's Chromium (one-time, ~120 MB)."""
    log = log or _noop
    try:
        process = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=1800,
            creationflags=0x08000000 if sys.platform.startswith("win") else 0)
    except Exception as exc:
        log(f"  browser download failed: {exc}", "error")
        return False
    for line in (process.stdout or "").splitlines()[-3:]:
        log("  " + line.strip(), "dim")
    if process.returncode != 0:
        log("  " + (process.stderr or "").strip()[-300:], "error")
    return process.returncode == 0


def browser_ready() -> bool:
    """Is a driveable browser present right now (no download needed)?"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    if default_browser_channel():
        return True                    # the user's own Chrome/Edge can drive
    try:
        playwright = sync_playwright().start()
    except Exception:
        return False
    try:
        return os.path.exists(playwright.chromium.executable_path)
    except Exception:
        return False
    finally:
        try:
            playwright.stop()
        except Exception:
            pass


def same_site(url: str, *hosts: str) -> bool:
    """Is `url` served by one of `hosts` (or a subdomain)?"""
    try:
        netloc = urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return False
    return any(netloc == h or netloc.endswith("." + h) for h in hosts)
