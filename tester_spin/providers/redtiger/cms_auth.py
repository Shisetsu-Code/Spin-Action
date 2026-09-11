from __future__ import annotations

import threading
from urllib.parse import urlparse


CMS_HOST = "cmsevo.com"
CMS_PATH_PREFIX = "/api/"


def authorization_from_headers(headers: dict[str, str]) -> str:
    """Return the CMS Authorization value case-insensitively.

    The credential is frontend deployment data. It is observed at runtime and is
    deliberately never persisted or hardcoded into Tester-Spin.
    """
    for key, value in (headers or {}).items():
        if str(key).casefold() != "authorization":
            continue
        candidate = str(value or "").strip()
        if candidate:
            return candidate
    return ""


def discover_cms_authorization(public_url: str, *, timeout_s: float = 30.0) -> str:
    """Observe the official Red Tiger frontend's authenticated CMS request.

    This is a transport bootstrap only. We do not scrape a token literal from a
    named webpack chunk and do not assume any static credential. Once observed,
    the caller can continue catalog enumeration directly over requests.Session.
    """
    timeout_ms = max(8_000, int(float(timeout_s) * 1000))
    found = ""
    found_event = threading.Event()

    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = None
    context = None
    try:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
            ),
        )
        page = context.new_page()

        def inspect_request(request) -> None:
            nonlocal found
            if found_event.is_set():
                return
            try:
                parsed = urlparse(str(request.url or ""))
            except Exception:
                return
            if parsed.netloc.casefold() != CMS_HOST:
                return
            if not parsed.path.startswith(CMS_PATH_PREFIX):
                return
            try:
                headers = request.all_headers()
            except Exception:
                headers = dict(request.headers or {})
            candidate = authorization_from_headers(headers)
            if candidate:
                found = candidate
                found_event.set()

        page.on("request", inspect_request)
        page.goto(public_url, wait_until="domcontentloaded", timeout=timeout_ms)

        deadline_ms = timeout_ms
        elapsed = 0
        step = 100
        while not found_event.is_set() and elapsed < deadline_ms:
            page.wait_for_timeout(step)
            elapsed += step

        if not found:
            raise RuntimeError(
                "Red Tiger CMS: el frontend oficial no emitió una request autenticada "
                "a cmsevo.com/api dentro del timeout."
            )
        return found
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        playwright.stop()
