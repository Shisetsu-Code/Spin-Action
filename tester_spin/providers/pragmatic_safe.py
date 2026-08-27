from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlencode, urlparse

import requests
from playwright.sync_api import Response, sync_playwright

from tester_spin.providers.base import Progress
from tester_spin.providers.pragmatic import (
    DROP_HEADERS,
    BrowserBootstrap,
    _fmt,
    _int,
    _parse_wire,
)
from tester_spin.providers.pragmatic_live import PragmaticProvider as _LivePragmaticProvider
from tester_spin.providers.pragmatic_modes import discover_modes


_PLAYWRIGHT_DISCOVERY_LOCK = threading.Lock()


def _safe_request_wire_text(request) -> str:
    """Return request body text without allowing Playwright to UTF-8 decode blindly.

    Some Pragmatic resources use request bodies that are not valid UTF-8. Accessing
    Request.post_data makes Playwright decode them as text; its exception rewriter
    can then mask the original UnicodeDecodeError as the misleading
    ``TypeError: function takes exactly 5 arguments (1 given)``.
    """
    try:
        raw = request.post_data_buffer
    except Exception:
        raw = None

    if raw:
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        try:
            return bytes(raw).decode("utf-8", errors="replace")
        except Exception:
            return str(raw)

    try:
        text = request.post_data
        if text:
            return str(text)
    except Exception:
        pass

    try:
        return urlparse(request.url).query
    except Exception:
        return ""


class PragmaticProvider(_LivePragmaticProvider):
    """Pragmatic provider with binary-safe doInit discovery."""

    def _browser_bootstrap(self, source_url: str, timeout_s: float, progress: Progress) -> BrowserBootstrap:
        init_exchange: dict[str, object] = {}
        launch_url = ""
        phase = "inicializando Playwright"

        # Playwright sync uses greenlets internally. Keep the short browser-only
        # discovery phase serialized; mode validation remains concurrent over HTTP.
        with _PLAYWRIGHT_DISCOVERY_LOCK:
            try:
                with sync_playwright() as playwright:
                    phase = "lanzando Chromium"
                    browser = playwright.chromium.launch(
                        headless=True,
                        args=["--mute-audio", "--disable-background-timer-throttling"],
                    )
                    phase = "creando BrowserContext"
                    context = browser.new_context(
                        viewport={"width": 1280, "height": 720},
                        locale="en-US",
                    )
                    try:
                        def on_response(response: Response) -> None:
                            nonlocal launch_url
                            try:
                                request = response.request
                                request_url = request.url
                                if "openGame.do" in request_url or "html5Game.do" in request_url:
                                    launch_url = request_url
                                if init_exchange:
                                    return
                                if request.resource_type not in {"xhr", "fetch", "document"}:
                                    return

                                raw_request = _safe_request_wire_text(request)
                                fields = _parse_wire(raw_request)
                                if str(fields.get("action") or "") != "doInit":
                                    return

                                try:
                                    raw_response = response.body()
                                except Exception:
                                    return

                                init_exchange.update(
                                    {
                                        "url": request_url,
                                        "request_raw": raw_request,
                                        "request": fields,
                                        "response_raw": raw_response,
                                        "response": _parse_wire(raw_response),
                                        "status": response.status,
                                        "headers": {
                                            key: value
                                            for key, value in request.headers.items()
                                            if key.lower() not in DROP_HEADERS
                                        },
                                    }
                                )
                            except Exception:
                                # Never let an unrelated resource break the page's
                                # network event dispatcher while waiting for doInit.
                                return

                        context.on("response", on_response)
                        phase = "abriendo página del juego"
                        page = context.new_page()
                        page.goto(
                            source_url,
                            wait_until="domcontentloaded",
                            timeout=int(timeout_s * 1000),
                        )
                        deadline = time.monotonic() + timeout_s
                        last_click = 0.0
                        phase = "esperando doInit"

                        while time.monotonic() < deadline and not init_exchange:
                            if time.monotonic() - last_click >= 0.8:
                                patterns = (
                                    r"accept\s+all",
                                    r"allow\s+all",
                                    r"i\s+agree",
                                    r"i\s+am\s+18",
                                    r"play\s+demo",
                                    r"play\s+now",
                                    r"jugar\s+demo",
                                    r"jugar\s+ahora",
                                    r"launch\s+game",
                                )
                                for frame in page.frames:
                                    for pattern in patterns:
                                        regex = re.compile(pattern, re.I)
                                        for role in ("button", "link"):
                                            try:
                                                locator = frame.get_by_role(role, name=regex)
                                                if locator.count() and locator.first.is_visible():
                                                    locator.first.scroll_into_view_if_needed(timeout=1_000)
                                                    try:
                                                        locator.first.click(timeout=1_500)
                                                    except Exception:
                                                        locator.first.click(timeout=1_500, force=True)
                                            except Exception:
                                                pass
                                last_click = time.monotonic()
                            page.wait_for_timeout(150)

                        if not init_exchange:
                            frame_urls = [frame.url for frame in page.frames if frame.url]
                            raise RuntimeError(
                                "No se capturó doInit del cliente oficial. "
                                f"Frames observados: {frame_urls[:8]}"
                            )

                        phase = "capturando cookies"
                        cookies = context.cookies()
                    finally:
                        context.close()
                        browser.close()
            except Exception as exc:
                raise RuntimeError(
                    f"bootstrap navegador falló en '{phase}': {type(exc).__name__}: {exc}"
                ) from exc

        init_request = dict(init_exchange["request"])  # type: ignore[arg-type]
        init_response = dict(init_exchange["response"])  # type: ignore[arg-type]
        symbol = str(init_request.get("symbol") or "")
        mgckey = str(init_request.get("mgckey") or "")
        cver = str(init_request.get("cver") or "") or None
        endpoint = str(init_exchange["url"])
        if not symbol or not mgckey or not endpoint:
            raise RuntimeError("doInit capturado pero sin symbol/mgckey/endpoint")

        catalog = discover_modes(init_response, requested_base_bet=self.base_bet)
        next_index = (_int(init_response.get("index")) or _int(init_request.get("index")) or 1) + 1
        next_counter = (_int(init_response.get("counter")) or _int(init_request.get("counter")) or 1) + 1
        spin_fields = {
            "action": "doSpin",
            "symbol": symbol,
            "c": _fmt(catalog.base_coin),
            "l": _fmt(catalog.base_scale),
            "sInfo": "t",
            "bl": "0",
            "index": str(next_index),
            "counter": str(next_counter),
            "repeat": "0",
            "mgckey": mgckey,
        }
        calibration_raw = urlencode(spin_fields)

        request_headers = dict(init_exchange.get("headers") or {})
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request_headers.setdefault("Accept", "*/*")

        progress(f"doInit OK: symbol={symbol}; enviando calibración HTTP a gameService")
        http = requests.Session()
        http.headers.update(request_headers)
        for cookie in cookies:
            name = str(cookie.get("name") or "")
            if not name:
                continue
            value = str(cookie.get("value") or "")
            domain = str(cookie.get("domain") or "") or None
            path = str(cookie.get("path") or "/")
            try:
                http.cookies.set(name, value, domain=domain, path=path)
            except Exception:
                http.cookies.set(name, value)

        try:
            response = http.post(
                endpoint,
                data=calibration_raw,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=timeout_s,
            )
            calibration_response_raw = response.content
            calibration_response = _parse_wire(calibration_response_raw)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"doSpin calibración HTTP {response.status_code}: "
                    f"{calibration_response_raw[:300]!r}"
                )
            server_error = (
                calibration_response.get("error")
                or calibration_response.get("err")
                or calibration_response.get("errorCode")
            )
            if server_error not in (None, "", "0"):
                raise RuntimeError(f"doSpin calibración server error={server_error}")
            progress(
                f"doSpin calibración OK: HTTP {response.status_code}, "
                f"na={calibration_response.get('na')!r}"
            )
        finally:
            http.close()

        return BrowserBootstrap(
            symbol=symbol,
            mgckey=mgckey,
            cver=cver,
            endpoint=endpoint,
            launch_url=launch_url or str(request_headers.get("referer") or source_url),
            headers=request_headers,
            cookies=list(cookies),
            init_request_raw=str(init_exchange["request_raw"]),
            init_response_raw=bytes(init_exchange["response_raw"]),  # type: ignore[arg-type]
            init_response=init_response,
            calibration_request_raw=calibration_raw,
            calibration_response_raw=calibration_response_raw,
            calibration_response=calibration_response,
        )
