from __future__ import annotations

import html
import re
import threading
from urllib.parse import parse_qs, urljoin, urlparse

from tester_spin.models import Game
from tester_spin.providers.base import GameCallback, Progress
from tester_spin.providers.pragmatic import (
    BrowserBootstrap,
    _extract_cver,
    _extract_launch_urls,
)
from tester_spin.providers.pragmatic_current import PragmaticProvider as _CurrentPragmaticProvider


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
    browser. Catalog traversal is HTTP pagination and game discovery/bootstrap is
    resolved from the game page plus Pragmatic HTTP endpoints.
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

            # Some WordPress/CDN configurations return the last page again instead
            # of 404. Two pages without a new slug are sufficient to terminate.
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

        # Prefer the provider's own launch URL because its gameSymbol parameter is
        # authoritative when present.
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

        # Last-resort heuristic: Pragmatic slot IDs commonly use a compact provider
        # prefix such as vs/cs followed by an alphanumeric identifier. Keep this
        # deliberately conservative to avoid treating arbitrary JS tokens as IDs.
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
