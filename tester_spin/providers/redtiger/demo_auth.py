from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode, urlparse, urlunparse

import requests


_API_KEY_CACHE: dict[tuple[str, str], str] = {}
_API_KEY_CACHE_LOCK = threading.Lock()


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


def _cache_key(public_url: str, demo_token_url: str) -> tuple[str, str]:
    public = urlparse(str(public_url or ""))
    target = urlparse(str(demo_token_url or ""))
    origin = f"{public.scheme.casefold()}://{public.netloc.casefold()}"
    return origin, target.netloc.casefold()


def _cached_api_key(public_url: str, demo_token_url: str) -> str:
    key = _cache_key(public_url, demo_token_url)
    with _API_KEY_CACHE_LOCK:
        return _API_KEY_CACHE.get(key, "")


def _remember_api_key(public_url: str, demo_token_url: str, api_key: str) -> None:
    value = str(api_key or "").strip()
    if not value:
        return
    key = _cache_key(public_url, demo_token_url)
    with _API_KEY_CACHE_LOCK:
        _API_KEY_CACHE[key] = value


def _forget_api_key(public_url: str, demo_token_url: str) -> None:
    key = _cache_key(public_url, demo_token_url)
    with _API_KEY_CACHE_LOCK:
        _API_KEY_CACHE.pop(key, None)


def demo_page_url(public_url: str, table_id: str) -> str:
    """Build the provider's public demo route from the catalog tableId.

    The public Red Tiger route is ``/demo/<tableId>?showNavbar=true``. Tenant is
    resolved internally by the site/router and must not be injected into the
    visible demo URL. No game title, runtime gameId, gserver hostname, session
    identifier or frontend secret is derived here.
    """
    table = str(table_id or "").strip()
    if not table:
        raise ValueError("Red Tiger demo auth: tableId vacío.")
    parsed = urlparse(str(public_url or ""))
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Red Tiger demo auth: URL pública inválida.")

    query = {"showNavbar": "true"}
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            f"/demo/{quote(table, safe='')}",
            "",
            urlencode(query),
            "",
        )
    )


def discover_demo_api_key(
    public_url: str,
    demo_token_url: str,
    table_id: str,
    *,
    timeout_s: float = 30.0,
) -> str:
    """Observe x-api-key from the official demo route for this tableId.

    Loading a public game detail page is insufficient: the captured Red Tiger
    frontend emits ``POST /api/v1/oss/token/demo`` only when the demo route is
    entered. We therefore navigate the provider-owned ``/demo/<tableId>`` route
    and inspect the request that the frontend itself sends to the configured
    demo-token host. The observed key is kept only in process memory.
    """
    target = urlparse(str(demo_token_url or ""))
    target_host = target.netloc.casefold()
    if not target_host:
        raise ValueError("Red Tiger demo auth: endpoint de token sin host.")

    launch_url = demo_page_url(public_url, table_id)
    timeout_ms = max(8_000, int(float(timeout_s) * 1000))
    found = ""
    found_event = threading.Event()

    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = None
    context = None
    try:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(locale="en-GB")
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
            if request.method.upper() != "POST":
                return
            if not parsed.path.rstrip("/").endswith("/api/v1/oss/token/demo"):
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
        page.goto(launch_url, wait_until="domcontentloaded", timeout=timeout_ms)

        elapsed = 0
        step = 100
        while not found_event.is_set() and elapsed < timeout_ms:
            page.wait_for_timeout(step)
            elapsed += step

        if not found:
            raise RuntimeError(
                "Red Tiger demo auth: la ruta demo oficial no emitió POST token/demo "
                "con x-api-key dentro del timeout."
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
    game = payload.get("game") if isinstance(payload, dict) else None
    table_id = str(game.get("tableId") or "").strip() if isinstance(game, dict) else ""
    if not table_id:
        raise ValueError("Red Tiger demo token: payload sin game.tableId.")

    cached = _cached_api_key(public_url, demo_token_url)
    if cached:
        session.headers["x-api-key"] = cached

    response = session.post(demo_token_url, json=payload, timeout=timeout_s)
    if response.status_code not in {401, 403}:
        response.raise_for_status()
        return response

    session.headers.pop("x-api-key", None)
    _forget_api_key(public_url, demo_token_url)

    if progress is not None:
        progress(
            "Red Tiger demo 401/403: obteniendo x-api-key desde el POST token/demo "
            "de la ruta demo oficial..."
        )

    api_key = discover_demo_api_key(
        public_url,
        demo_token_url,
        table_id,
        timeout_s=max(15.0, float(timeout_s)),
    )
    _remember_api_key(public_url, demo_token_url, api_key)
    session.headers["x-api-key"] = api_key
    response = session.post(demo_token_url, json=payload, timeout=timeout_s)
    if response.status_code in {401, 403}:
        _forget_api_key(public_url, demo_token_url)
    response.raise_for_status()

    if progress is not None:
        progress(
            "Red Tiger demo: x-api-key observada y cacheada sólo en memoria; "
            "token demo HTTP OK."
        )
    return response
