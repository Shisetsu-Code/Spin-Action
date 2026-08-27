from __future__ import annotations

import threading
from urllib.parse import urljoin

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import GameCallback, Progress
from tester_spin.providers.pragmatic import PragmaticProvider as _PragmaticProvider


class PragmaticProvider(_PragmaticProvider):
    """Pragmatic adapter with streaming catalog callbacks for the GUI."""

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
    ) -> list[Game]:
        by_slug: dict[str, Game] = {}
        no_new_pages = 0
        for page_no in range(1, max(1, int(max_pages)) + 1):
            if stop_event.is_set():
                break
            page_url = self.catalog_url if page_no == 1 else urljoin(self.catalog_url, f"page/{page_no}/")
            progress(f"Catálogo Pragmatic: página {page_no} — {page_url}")
            response = self.http.get(page_url, timeout=30.0)
            if response.status_code == 404:
                break
            response.raise_for_status()
            page_games = self._extract_catalog_page(response.text, response.url)
            new_count = 0
            for game in page_games:
                if game.slug not in by_slug:
                    by_slug[game.slug] = game
                    new_count += 1
                    try:
                        self._persist_catalog_artifacts(game, response.url)
                    except Exception as exc:
                        progress(f"[{game.name}] miniatura/metadata: {type(exc).__name__}: {exc}")
                    if on_game is not None:
                        on_game(game)
                    progress(f"  + {game.name} — {game.url}")
                else:
                    current = by_slug[game.slug]
                    if not current.thumbnail_url and game.thumbnail_url:
                        current.thumbnail_url = game.thumbnail_url

            progress(
                f"Página {page_no}: {len(page_games)} enlaces de juego, "
                f"{new_count} nuevos, total={len(by_slug)}"
            )
            if new_count == 0:
                no_new_pages += 1
            else:
                no_new_pages = 0
            if no_new_pages >= 2:
                break

        games = sorted(by_slug.values(), key=lambda item: item.name.casefold())
        self._write_catalog_index(games)
        progress(f"Catálogo Pragmatic terminado: {len(games)} juegos únicos")
        return games

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
            self._write_json(__import__("pathlib").Path(result.run_dir) / "result.json", result.to_dict())
            self._record_last_test_in_game_json(game, result)
        return result
