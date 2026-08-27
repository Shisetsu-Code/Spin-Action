from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import GameCallback, Progress
from tester_spin.providers.pragmatic import HttpBootstrap, PragmaticProvider as _PragmaticProvider


class PragmaticProvider(_PragmaticProvider):
    """Pragmatic adapter with streaming catalog callbacks and clean test sessions."""

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
    ) -> list[Game]:
        """Load the dynamic Pragmatic catalog by repeatedly pressing Load More Games.

        ``max_pages`` is kept in the provider interface for compatibility, but for
        Pragmatic it means the maximum number of dynamic load batches/clicks.
        Games are persisted and streamed to the GUI as soon as they appear.
        """
        by_slug: dict[str, Game] = {}
        max_loads = max(1, int(max_pages))

        def ingest_visible(html: str, page_url: str, batch_no: int) -> int:
            visible = self._extract_catalog_page(html, page_url)
            new_count = 0
            for game in visible:
                existing = by_slug.get(game.slug)
                if existing is not None:
                    if not existing.thumbnail_url and game.thumbnail_url:
                        existing.thumbnail_url = game.thumbnail_url
                    continue

                by_slug[game.slug] = game
                new_count += 1
                try:
                    self._persist_catalog_artifacts(game, page_url)
                except Exception as exc:
                    progress(f"[{game.name}] miniatura/metadata: {type(exc).__name__}: {exc}")
                if on_game is not None:
                    on_game(game)
                progress(f"  + [{batch_no}] {game.name} — {game.url}")
            return new_count

        progress(f"Catálogo Pragmatic dinámico — {self.catalog_url}")
        progress("Estrategia: cargar DOM inicial y pulsar 'Load More Games' hasta agotar el catálogo.")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="en-US",
            )
            page = context.new_page()
            page.goto(self.catalog_url, wait_until="domcontentloaded", timeout=60_000)

            # Cookie/consent banners can overlap the Load More button.
            for label in ("Accept All", "Accept all", "Allow all", "I agree"):
                try:
                    button = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
                    if button.count() and button.first.is_visible():
                        button.first.click(timeout=1_500)
                        break
                except Exception:
                    pass

            page.wait_for_timeout(800)
            initial_new = ingest_visible(page.content(), page.url, 0)
            progress(f"Lote inicial: {initial_new} juegos nuevos; total={len(by_slug)}")

            loads_done = 0
            consecutive_stalls = 0
            while loads_done < max_loads and not stop_event.is_set():
                try:
                    load_more = page.get_by_role(
                        "button",
                        name=re.compile(r"load\s+more\s+games", re.I),
                    )
                    if load_more.count() == 0 or not load_more.first.is_visible():
                        progress("El botón 'Load More Games' ya no está visible: catálogo agotado.")
                        break
                except Exception:
                    progress("No se encontró 'Load More Games': catálogo agotado.")
                    break

                before_total = len(by_slug)
                before_visible = len(self._extract_catalog_page(page.content(), page.url))
                loads_done += 1
                progress(
                    f"Carga dinámica {loads_done}/{max_loads}: pulsando 'Load More Games' "
                    f"(visibles={before_visible}, guardados={before_total})"
                )

                try:
                    load_more.first.scroll_into_view_if_needed(timeout=5_000)
                    load_more.first.click(timeout=10_000)
                except Exception as exc:
                    progress(f"Click Load More falló: {type(exc).__name__}: {exc}")
                    break

                # The site appends cards asynchronously. Wait for the number of
                # discovered game links to grow instead of sleeping a fixed long delay.
                deadline = time.monotonic() + 12.0
                visible_count = before_visible
                while time.monotonic() < deadline and not stop_event.is_set():
                    page.wait_for_timeout(250)
                    visible_count = len(self._extract_catalog_page(page.content(), page.url))
                    if visible_count > before_visible:
                        break

                new_count = ingest_visible(page.content(), page.url, loads_done)
                progress(
                    f"Carga {loads_done}: visibles={visible_count}, "
                    f"nuevos={new_count}, total={len(by_slug)}"
                )

                if new_count == 0:
                    consecutive_stalls += 1
                    if consecutive_stalls >= 2:
                        progress(
                            "Dos cargas consecutivas sin juegos nuevos; se detiene para evitar un bucle infinito."
                        )
                        break
                else:
                    consecutive_stalls = 0

            context.close()
            browser.close()

        games = sorted(by_slug.values(), key=lambda item: item.name.casefold())
        self._write_catalog_index(games)
        progress(
            f"Catálogo Pragmatic terminado: {len(games)} juegos únicos; "
            f"{loads_done} carga(s) dinámica(s)."
        )
        return games

    def _http_bootstrap(
        self,
        source_url: str,
        symbol: str,
        cver: str | None,
        base_bet: float,
        timeout_s: float,
    ) -> HttpBootstrap:
        # The bootstrap includes one base calibration spin. Never start a target
        # mode while that calibration is waiting for collect/free-spin/another
        # provider transition. Re-open until calibration lands in an idle state.
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
