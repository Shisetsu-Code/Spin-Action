from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import requests


def api_key_from_headers(headers: dict[str, str]) -> str:
    """Return the provider x-api-key value case-insensitively.

    The key is frontend deployment data, not a Red Tiger protocol constant. It is
    deliberately observed at runtime and never persisted or hardcoded.
    """
    for key, value in (headers or {}).items():
        if str(key).casefold() != "x-api-key":
            continue
        candidate = str(value or "").strip()
        if candidate:
            return candidate
    return ""


def discover_demo_api_key(
    public_url: str,
    demo_token_url: str,
    *,
    timeout_s: float = 30.0,
) -> str:
    """Observe the official frontend's API key for the current demo deployment.

    We intentionally do not search source bundles for a literal. The browser loads
    the public game page and we inspect only requests to the same host used by the
    configured demo-token endpoint. Any request carrying x-api-key demonstrates
    the current frontend contract and can bootstrap the direct HTTP token request.
    """
    target = urlparse(str(demo_token_url or ""))
    target_host = target.netloc.casefold()
    if not target_host:
        raise ValueError("Red Tiger demo auth: endpoint de token sin host.")

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
            locale="en-GB",
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
            if parsed.netloc.casefold() != target_host:
                return
            try:
                headers = request.all_headers()
            except Exception:
                headers = dict(request.headers or {})
            candidate = api_key_from_headers(headers)
            if candidate:
                found = candidate
                found_event.set()

        page.on("request", inspect_request)
        page.goto(public_url, wait_until="domcontentloaded", timeout=timeout_ms)

        elapsed = 0
        step = 100
        while not found_event.is_set() and elapsed < timeout_ms:
            page.wait_for_timeout(step)
            elapsed += step

        if not found:
            raise RuntimeError(
                "Red Tiger demo auth: el frontend oficial no emitió una request "
                "con x-api-key al host del servicio demo dentro del timeout."
            )
        return found
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        playwright.stop()


def post_demo_token(
    session: requests.Session,
    demo_token_url: str,
    public_url: str,
    payload: dict[str, Any],
    *,
    timeout_s: float,
    progress: Callable[[str], None] | None = None,
) -> requests.Response:
    """POST demo token, dynamically bootstrapping x-api-key on auth failure."""
    response = session.post(demo_token_url, json=payload, timeout=timeout_s)
    if response.status_code not in {401, 403}:
        response.raise_for_status()
        return response

    if progress is not None:
        progress(
            "Red Tiger demo 401/403: obteniendo x-api-key desde una request real "
            "del frontend oficial..."
        )

    api_key = discover_demo_api_key(
        public_url,
        demo_token_url,
        timeout_s=max(15.0, float(timeout_s)),
    )
    session.headers["x-api-key"] = api_key
    response = session.post(demo_token_url, json=payload, timeout=timeout_s)
    response.raise_for_status()

    if progress is not None:
        progress(
            "Red Tiger demo: x-api-key del frontend reutilizada sólo en memoria; "
            "token demo HTTP OK."
        )
    return response
