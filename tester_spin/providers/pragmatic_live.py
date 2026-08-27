from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests
from playwright.sync_api import Response, sync_playwright

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import GameCallback, Progress
from tester_spin.providers.pragmatic import (
    DROP_HEADERS,
    BrowserBootstrap,
    HttpBootstrap,
    PragmaticProvider as _PragmaticProvider,
    _fmt,
    _int,
    _parse_wire,
)
from tester_spin.providers.pragmatic_modes import discover_modes


class PragmaticProvider(_PragmaticProvider):
    """Pragmatic adapter with dynamic catalog loading and HTTP protocol testing."""

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
    ) -> list[Game]:
        """Load the catalog by pressing the site's dynamic Load More control.

        ``max_pages`` is retained for the common provider interface; for Pragmatic
        it means maximum dynamic loads/clicks, not numbered HTML pages.
        """
        by_slug: dict[str, Game] = {}
        max_loads = max(1, int(max_pages))

        def ingest(html_text: str, page_url: str, batch: int) -> int:
            new_count = 0
            for game in self._extract_catalog_page(html_text, page_url):
                current = by_slug.get(game.slug)
                if current is not None:
                    if not current.thumbnail_url and game.thumbnail_url:
                        current.thumbnail_url = game.thumbnail_url
                    continue
                by_slug[game.slug] = game
                new_count += 1
                try:
                    self._persist_catalog_artifacts(game, page_url)
                except Exception as exc:
                    progress(f"[{game.name}] miniatura/metadata: {type(exc).__name__}: {exc}")
                if on_game is not None:
                    on_game(game)
                progress(f"  + [{batch}] {game.name} — {game.url}")
            return new_count

        def find_load_more(page):
            regex = re.compile(r"load\s+more(?:\s+games)?", re.I)
            candidates = []
            for role in ("button", "link"):
                try:
                    candidates.append(page.get_by_role(role, name=regex))
                except Exception:
                    pass
            try:
                candidates.append(page.get_by_text(regex, exact=False))
            except Exception:
                pass
            for locator in candidates:
                try:
                    count = locator.count()
                except Exception:
                    continue
                for idx in range(min(count, 5)):
                    item = locator.nth(idx)
                    try:
                        if item.is_visible():
                            return item
                    except Exception:
                        continue
            return None

        progress(f"Catálogo Pragmatic dinámico — {self.catalog_url}")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page = context.new_page()
            try:
                page.goto(self.catalog_url, wait_until="domcontentloaded", timeout=60_000)
                for label in ("Accept All", "Accept all", "Allow all", "I agree"):
                    try:
                        button = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
                        if button.count() and button.first.is_visible():
                            button.first.click(timeout=1_500)
                            break
                    except Exception:
                        pass

                page.wait_for_timeout(800)
                initial = ingest(page.content(), page.url, 0)
                progress(f"Lote inicial: {initial} juegos nuevos; total={len(by_slug)}")

                loads_done = 0
                stalls = 0
                while loads_done < max_loads and not stop_event.is_set():
                    load_more = find_load_more(page)
                    if load_more is None:
                        progress("'Load More Games' ya no está visible: catálogo agotado.")
                        break

                    before_visible = len(self._extract_catalog_page(page.content(), page.url))
                    loads_done += 1
                    progress(
                        f"Carga dinámica {loads_done}/{max_loads}: "
                        f"visibles={before_visible}, guardados={len(by_slug)}"
                    )

                    clicked = False
                    errors: list[str] = []
                    for strategy in ("normal", "force", "javascript"):
                        try:
                            load_more.scroll_into_view_if_needed(timeout=3_000)
                            if strategy == "normal":
                                load_more.click(timeout=4_000)
                            elif strategy == "force":
                                load_more.click(timeout=4_000, force=True)
                            else:
                                load_more.evaluate("el => el.click()")
                            clicked = True
                            break
                        except Exception as exc:
                            errors.append(f"{strategy}:{type(exc).__name__}")
                    if not clicked:
                        progress("Load More no pudo activarse: " + ", ".join(errors))
                        break

                    deadline = time.monotonic() + 15.0
                    visible_count = before_visible
                    while time.monotonic() < deadline and not stop_event.is_set():
                        page.wait_for_timeout(250)
                        visible_count = len(self._extract_catalog_page(page.content(), page.url))
                        if visible_count > before_visible:
                            break

                    new_count = ingest(page.content(), page.url, loads_done)
                    progress(
                        f"Carga {loads_done}: visibles={visible_count}, "
                        f"nuevos={new_count}, total={len(by_slug)}"
                    )
                    if new_count == 0:
                        stalls += 1
                        if stalls >= 2:
                            progress("Dos cargas sin juegos nuevos; catálogo detenido.")
                            break
                    else:
                        stalls = 0
            finally:
                context.close()
                browser.close()

        games = sorted(by_slug.values(), key=lambda item: item.name.casefold())
        self._write_catalog_index(games)
        progress(f"Catálogo Pragmatic terminado: {len(games)} juegos únicos.")
        return games

    def _browser_bootstrap(self, source_url: str, timeout_s: float, progress: Progress) -> BrowserBootstrap:
        """Capture doInit with Chromium, then calibrate doSpin with requests.

        The browser is responsible only for establishing the official game session.
        The actual protocol calibration is sent with requests using the exact
        doInit endpoint plus cookies/headers copied from that browser session.
        """
        init_exchange: dict[str, object] = {}
        launch_url = ""

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--mute-audio", "--disable-background-timer-throttling"],
            )
            context = browser.new_context(viewport={"width": 1280, "height": 720}, locale="en-US")
            try:
                def on_response(response: Response) -> None:
                    nonlocal launch_url
                    request = response.request
                    if "openGame.do" in request.url or "html5Game.do" in request.url:
                        launch_url = request.url
                    if init_exchange or request.resource_type not in {"xhr", "fetch", "document"}:
                        return
                    raw_request = request.post_data or urlparse(request.url).query
                    fields = _parse_wire(raw_request)
                    if str(fields.get("action") or "") != "doInit":
                        return
                    try:
                        raw_response = response.body()
                    except Exception:
                        return
                    init_exchange.update(
                        {
                            "url": request.url,
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

                context.on("response", on_response)
                page = context.new_page()
                page.goto(source_url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
                deadline = time.monotonic() + timeout_s
                last_click = 0.0

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
                cookies = context.cookies()

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
            finally:
                context.close()
                browser.close()

    def _http_bootstrap(
        self,
        source_url: str,
        symbol: str,
        cver: str | None,
        base_bet: float,
        timeout_s: float,
    ) -> HttpBootstrap:
        # Each target mode must start from an idle session. A calibration spin can
        # itself enter collect/free-spin state, so discard it and retry when needed.
        last_state = ""
        for _ in range(12):
            bootstrap = super()._http_bootstrap(source_url, symbol, cver, base_bet, timeout_s)
            last_state = str(bootstrap.calibration_response.get("na") or "")
            feature_active = self._feature_active(bootstrap.calibration_response)
            if last_state in {"", "s"} and not feature_active:
                return bootstrap
            bootstrap.session.close()
        raise RuntimeError(
            f"no se obtuvo una sesión idle tras 12 calibraciones; último na={last_state!r}"
        )

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        result = super().test_game(
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )
        warnings = [attempt.warning for attempt in result.attempts if attempt.warning]
        if warnings and result.status == "OK":
            result.status = "PARCIAL"
            result.error = (
                f"{len(warnings)} intento(s) respondieron pero terminaron en estados de continuación "
                "aún no automatizados; RAW preservado para implementar esos estados."
            )
            self._write_json(Path(result.run_dir) / "result.json", result.to_dict())
            self._record_last_test_in_game_json(game, result)
        return result
