from __future__ import annotations

import html
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from tester_spin.models import Game, SpinAttempt
from tester_spin.providers.base import GameCallback, Progress
from tester_spin.providers.pragmatic import (
    BrowserBootstrap,
    HttpBootstrap,
    _extract_cver,
    _extract_launch_urls,
    _fmt,
    _int,
)
from tester_spin.providers.pragmatic_current import PragmaticProvider as _CurrentPragmaticProvider
from tester_spin.providers.pragmatic_modes import PragmaticMode, PragmaticModeCatalog


_SYMBOL_PATTERNS = (
    re.compile(r"[?&]gameSymbol=([A-Za-z0-9_-]+)", re.I),
    re.compile(r"[\"']gameSymbol[\"']\s*[:=]\s*[\"']([A-Za-z0-9_-]+)", re.I),
    re.compile(r"[\"']game_symbol[\"']\s*[:=]\s*[\"']([A-Za-z0-9_-]+)", re.I),
    re.compile(r"data-game-symbol\s*=\s*[\"']([A-Za-z0-9_-]+)", re.I),
    re.compile(r"[\"']symbol[\"']\s*[:=]\s*[\"']((?:vs|cs|bn|rng)[A-Za-z0-9_-]+)", re.I),
)


class PragmaticProvider(_CurrentPragmaticProvider):
    """Endpoint-first Pragmatic adapter.

    The normal execution path does not click UI controls and does not need a
    browser. Catalog traversal is HTTP pagination, game discovery/bootstrap is
    HTTP-only, and game state transitions are sent directly to gameService.
    """

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
    ) -> list[Game]:
        by_slug: dict[str, Game] = {}
        duplicate_pages = 0
        max_pages = max(1, int(max_pages))

        progress(f"Catálogo Pragmatic por HTTP — {self.catalog_url}")
        progress("Modo endpoint-first: no se usa Playwright ni clicks visuales.")

        for page_no in range(1, max_pages + 1):
            if stop_event.is_set():
                break

            page_url = self.catalog_url if page_no == 1 else urljoin(self.catalog_url, f"page/{page_no}/")
            progress(f"HTTP catálogo {page_no}/{max_pages}: GET {page_url}")
            response = self.http.get(page_url, timeout=30.0, allow_redirects=True)

            if response.status_code == 404:
                progress(f"Página HTTP {page_no}: 404; catálogo agotado.")
                break
            response.raise_for_status()

            page_games = self._extract_catalog_page(response.text, response.url)
            new_count = 0
            for game in page_games:
                current = by_slug.get(game.slug)
                if current is not None:
                    if not current.thumbnail_url and game.thumbnail_url:
                        current.thumbnail_url = game.thumbnail_url
                    continue

                by_slug[game.slug] = game
                new_count += 1
                try:
                    self._persist_catalog_artifacts(game, response.url)
                except Exception as exc:
                    progress(f"[{game.name}] miniatura/metadata: {type(exc).__name__}: {exc}")
                if on_game is not None:
                    on_game(game)
                progress(f"  + [{page_no}] {game.name} — {game.url}")

            progress(
                f"Página HTTP {page_no}: encontrados={len(page_games)}, "
                f"nuevos={new_count}, total={len(by_slug)}"
            )

            if new_count == 0:
                duplicate_pages += 1
            else:
                duplicate_pages = 0

            if duplicate_pages >= 2:
                progress("Dos páginas HTTP consecutivas sin juegos nuevos; catálogo agotado.")
                break

        games = sorted(by_slug.values(), key=lambda item: item.name.casefold())
        self._write_catalog_index(games)
        progress(f"Catálogo Pragmatic terminado por HTTP: {len(games)} juegos únicos.")
        return games

    def _resolve_symbol_http(self, source_url: str, timeout_s: float) -> tuple[str, str | None, str]:
        response = self.http.get(source_url, timeout=timeout_s, allow_redirects=True)
        response.raise_for_status()
        text = html.unescape(response.text)
        final_url = response.url

        for launch_url in _extract_launch_urls(text, final_url):
            parsed = urlparse(launch_url)
            symbol = (parse_qs(parsed.query).get("gameSymbol") or [""])[0].strip()
            if symbol:
                return symbol, _extract_cver(launch_url) or _extract_cver(text), launch_url

        for pattern in _SYMBOL_PATTERNS:
            match = pattern.search(text)
            if match:
                symbol = match.group(1).strip()
                if symbol:
                    return symbol, _extract_cver(text), final_url

        candidates = re.findall(r"\b(?:vs|cs|bn|rng)[A-Za-z0-9]{3,40}\b", text, flags=re.I)
        if candidates:
            return candidates[0], _extract_cver(text), final_url

        raise RuntimeError("la página HTTP del juego no expone un provider_internal_id reconocible")

    def _browser_bootstrap(self, source_url: str, timeout_s: float, progress: Progress) -> BrowserBootstrap:
        """Compatibility hook implemented entirely through HTTP endpoints."""
        progress("Resolviendo ID interno por HTTP; navegador deshabilitado para este flujo...")
        symbol, cver, source_or_launch = self._resolve_symbol_http(source_url, timeout_s)
        progress(f"ID interno resuelto por HTTP: symbol={symbol}")

        bootstrap = self._http_bootstrap(source_url, symbol, cver, self.base_bet, timeout_s)
        try:
            cookies = [
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                }
                for cookie in bootstrap.session.cookies
            ]
            headers = {str(k): str(v) for k, v in bootstrap.session.headers.items()}
            return BrowserBootstrap(
                symbol=bootstrap.symbol,
                mgckey=bootstrap.mgckey,
                cver=bootstrap.cver,
                endpoint=bootstrap.endpoint,
                launch_url=bootstrap.launch_url or source_or_launch,
                headers=headers,
                cookies=cookies,
                init_request_raw=bootstrap.init_request_raw,
                init_response_raw=bootstrap.init_response_raw,
                init_response=dict(bootstrap.init_response),
                calibration_request_raw=bootstrap.calibration_request_raw,
                calibration_response_raw=bootstrap.calibration_response_raw,
                calibration_response=dict(bootstrap.calibration_response),
            )
        finally:
            bootstrap.session.close()

    @staticmethod
    def _server_error(fields: dict[str, str]) -> str:
        value = fields.get("error") or fields.get("err") or fields.get("errorCode")
        return "" if value in (None, "", "0") else str(value)

    def _test_mode_once(
        self,
        game: Game,
        *,
        symbol: str,
        cver: str | None,
        mode: PragmaticMode,
        catalog: PragmaticModeCatalog,
        attempt_number: int,
        repetition: int,
        run_root: Path,
        timeout_s: float,
    ) -> SpinAttempt:
        attempt_root = run_root / mode.id / f"attempt-{repetition:04d}"
        attempt_root.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        bootstrap: HttpBootstrap | None = None

        try:
            bootstrap = self._http_bootstrap(game.url, symbol, cver, catalog.base_bet, timeout_s)
            self._write_http_bootstrap(attempt_root, bootstrap)

            index = (
                _int(bootstrap.calibration_response.get("index"))
                or _int(bootstrap.init_response.get("index"))
                or 1
            ) + 1
            counter = (
                _int(bootstrap.calibration_response.get("counter"))
                or _int(bootstrap.init_response.get("counter"))
                or 1
            ) + 1

            fields = dict(bootstrap.spin_template)
            fields.update(
                {
                    "action": "doSpin",
                    "symbol": symbol,
                    "c": _fmt(catalog.base_coin),
                    "l": _fmt(catalog.base_scale),
                    "bl": str(mode.provider_bl or 0),
                    "index": str(index),
                    "counter": str(counter),
                    "repeat": fields.get("repeat", "0") or "0",
                    "mgckey": bootstrap.mgckey,
                }
            )
            if mode.kind == "PURCHASE":
                fields["pur"] = str(mode.provider_pur)
            else:
                fields.pop("pur", None)

            status_code, _, last, _ = self._post_and_store(
                bootstrap,
                fields,
                attempt_root,
                step=0,
                label="entry",
                timeout_s=timeout_s,
            )
            if status_code >= 400:
                raise RuntimeError(f"HTTP {status_code}")
            error = self._server_error(last)
            if error:
                raise RuntimeError(f"server error={error}")

            wire_steps = 1
            terminal = False
            warning = ""
            in_bonus = False

            while wire_steps < 128:
                na = str(last.get("na") or "").strip().lower()

                if na == "b":
                    # Pragmatic's documented flow enters the bonus game with
                    # doBonus. Subsequent bonus steps can request b again.
                    in_bonus = True
                    index = (_int(last.get("index")) or index) + 1
                    counter = (_int(last.get("counter")) or counter) + 1
                    bonus_fields = {
                        "symbol": symbol,
                        "action": "doBonus",
                        "index": str(index),
                        "counter": str(counter),
                        "repeat": "0",
                        "mgckey": bootstrap.mgckey,
                    }
                    status_code, _, last, _ = self._post_and_store(
                        bootstrap,
                        bonus_fields,
                        attempt_root,
                        step=wire_steps,
                        label="bonus",
                        timeout_s=timeout_s,
                    )
                    wire_steps += 1
                    if status_code >= 400:
                        raise RuntimeError(f"doBonus HTTP {status_code}")
                    error = self._server_error(last)
                    if error:
                        raise RuntimeError(f"doBonus server error={error}")
                    continue

                if na in {"cb", "bc"} or (na == "c" and in_bonus):
                    index = (_int(last.get("index")) or index) + 1
                    counter = (_int(last.get("counter")) or counter) + 1
                    collect_bonus_fields = {
                        "symbol": symbol,
                        "action": "doCollectBonus",
                        "index": str(index),
                        "counter": str(counter),
                        "repeat": "0",
                        "mgckey": bootstrap.mgckey,
                    }
                    status_code, _, last, _ = self._post_and_store(
                        bootstrap,
                        collect_bonus_fields,
                        attempt_root,
                        step=wire_steps,
                        label="collect-bonus",
                        timeout_s=timeout_s,
                    )
                    wire_steps += 1
                    if status_code >= 400:
                        raise RuntimeError(f"doCollectBonus HTTP {status_code}")
                    error = self._server_error(last)
                    if error:
                        raise RuntimeError(f"doCollectBonus server error={error}")
                    # Collection is a terminal operation unless the server explicitly
                    # tells us to continue with another state.
                    next_na = str(last.get("na") or "").strip().lower()
                    if next_na in {"", "s"} and not self._feature_active(last):
                        terminal = True
                        break
                    continue

                if na == "c":
                    index = (_int(last.get("index")) or index) + 1
                    counter = (_int(last.get("counter")) or counter) + 1
                    collect_fields = {
                        "symbol": symbol,
                        "action": "doCollect",
                        "index": str(index),
                        "counter": str(counter),
                        "repeat": "0",
                        "mgckey": bootstrap.mgckey,
                    }
                    status_code, _, last, _ = self._post_and_store(
                        bootstrap,
                        collect_fields,
                        attempt_root,
                        step=wire_steps,
                        label="collect",
                        timeout_s=timeout_s,
                    )
                    wire_steps += 1
                    if status_code >= 400:
                        raise RuntimeError(f"doCollect HTTP {status_code}")
                    error = self._server_error(last)
                    if error:
                        raise RuntimeError(f"doCollect server error={error}")
                    terminal = True
                    break

                if na == "s" and self._feature_active(last):
                    index = (_int(last.get("index")) or index) + 1
                    counter = (_int(last.get("counter")) or counter) + 1
                    continuation = dict(bootstrap.spin_template)
                    continuation.update(
                        {
                            "action": "doSpin",
                            "symbol": symbol,
                            "c": _fmt(catalog.base_coin),
                            "l": _fmt(catalog.base_scale),
                            "bl": str(mode.provider_bl or 0),
                            "index": str(index),
                            "counter": str(counter),
                            "repeat": "0",
                            "mgckey": bootstrap.mgckey,
                        }
                    )
                    continuation.pop("pur", None)
                    status_code, _, last, _ = self._post_and_store(
                        bootstrap,
                        continuation,
                        attempt_root,
                        step=wire_steps,
                        label="continuation-spin",
                        timeout_s=timeout_s,
                    )
                    wire_steps += 1
                    if status_code >= 400:
                        raise RuntimeError(f"continuation doSpin HTTP {status_code}")
                    error = self._server_error(last)
                    if error:
                        raise RuntimeError(f"continuation doSpin server error={error}")
                    continue

                if na in {"", "s"}:
                    terminal = True
                    break

                warning = f"estado de continuación no automatizado: na={na!r}; RAW preservado"
                break

            if not terminal and not warning and wire_steps >= 128:
                warning = "límite de 128 pasos alcanzado; RAW preservado"

            elapsed_ms = (time.monotonic() - started) * 1000.0
            attempt = SpinAttempt(
                number=attempt_number,
                ok=True,
                mode_id=mode.id,
                mode_kind=mode.kind,
                provider_bl=mode.provider_bl,
                provider_pur=mode.provider_pur,
                status_code=status_code,
                elapsed_ms=elapsed_ms,
                symbol=symbol,
                endpoint=bootstrap.endpoint,
                na=str(last.get("na") or ""),
                terminal=terminal,
                wire_steps=wire_steps,
                warning=warning,
                artifact_dir=str(attempt_root),
            )
            self._write_json(attempt_root / "attempt.json", attempt.to_dict())
            return attempt

        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            attempt = SpinAttempt(
                number=attempt_number,
                ok=False,
                mode_id=mode.id,
                mode_kind=mode.kind,
                provider_bl=mode.provider_bl,
                provider_pur=mode.provider_pur,
                elapsed_ms=elapsed_ms,
                symbol=symbol,
                endpoint="" if bootstrap is None else bootstrap.endpoint,
                error=f"{type(exc).__name__}: {exc}",
                artifact_dir=str(attempt_root),
            )
            self._write_json(attempt_root / "attempt.json", attempt.to_dict())
            return attempt
        finally:
            if bootstrap is not None:
                bootstrap.session.close()
