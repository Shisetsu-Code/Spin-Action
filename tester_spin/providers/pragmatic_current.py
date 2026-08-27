from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import GameCallback, Progress
from tester_spin.providers.pragmatic_safe import PragmaticProvider as _SafePragmaticProvider


class PragmaticProvider(_SafePragmaticProvider):
    """Current Pragmatic adapter.

    Keeps the already working binary-safe protocol bootstrap, while tightening two
    pieces that are independent of the game protocol itself:

    * catalog expansion is driven by the site's exact ``Load More Games`` control;
    * a mode only counts as completed when its state machine reaches a terminal
      state. HTTP 200 with an unknown continuation (for example ``na=b``) remains
      a response, but not a completed round.
    """

    LOAD_MORE_TEXT = "Load More Games"

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
    ) -> list[Game]:
        by_slug: dict[str, Game] = {}
        max_loads = max(1, int(max_pages))

        def ingest(batch: int, page) -> int:
            new_count = 0
            for game in self._extract_catalog_page(page.content(), page.url):
                current = by_slug.get(game.slug)
                if current is not None:
                    if not current.thumbnail_url and game.thumbnail_url:
                        current.thumbnail_url = game.thumbnail_url
                    continue

                by_slug[game.slug] = game
                new_count += 1
                try:
                    self._persist_catalog_artifacts(game, page.url)
                except Exception as exc:
                    progress(f"[{game.name}] miniatura/metadata: {type(exc).__name__}: {exc}")
                if on_game is not None:
                    on_game(game)
                progress(f"  + [{batch}] {game.name} — {game.url}")
            return new_count

        def game_count(page) -> int:
            return len(self._extract_catalog_page(page.content(), page.url))

        def exact_load_more(page):
            exact = re.compile(r"^\s*Load More Games\s*$", re.I)
            candidates = (
                page.get_by_role("button", name=self.LOAD_MORE_TEXT, exact=True),
                page.get_by_role("link", name=self.LOAD_MORE_TEXT, exact=True),
                page.locator("button, a, [role='button']").filter(has_text=exact),
                page.get_by_text(self.LOAD_MORE_TEXT, exact=True),
            )
            for locator in candidates:
                try:
                    count = locator.count()
                except Exception:
                    continue
                for idx in range(min(count, 10)):
                    item = locator.nth(idx)
                    try:
                        if item.is_visible():
                            return item
                    except Exception:
                        continue
            return None

        def click_load_more(page, locator) -> str:
            """Activate the exact control and return the strategy that worked."""
            try:
                locator.scroll_into_view_if_needed(timeout=3_000)
            except Exception:
                pass

            try:
                locator.click(timeout=4_000)
                return "playwright"
            except Exception:
                pass

            try:
                locator.click(timeout=4_000, force=True)
                return "playwright-force"
            except Exception:
                pass

            # get_by_text() may resolve to a nested span. Click the nearest actual
            # interactive ancestor instead of the text node itself.
            try:
                locator.evaluate(
                    """el => {
                        const target = el.closest('button,a,[role="button"]') || el;
                        target.scrollIntoView({block: 'center'});
                        target.click();
                    }"""
                )
                return "closest-click"
            except Exception:
                pass

            # Last resort: locate the control by the exact visible English label in
            # the live DOM. This deliberately does not match generic "Load More".
            clicked = page.evaluate(
                """() => {
                    const wanted = 'Load More Games';
                    const nodes = Array.from(document.querySelectorAll(
                        'button,a,[role="button"],span,div'
                    ));
                    const el = nodes.find(node =>
                        (node.textContent || '').trim() === wanted &&
                        node.getClientRects().length > 0
                    );
                    if (!el) return false;
                    const target = el.closest('button,a,[role="button"]') || el;
                    target.scrollIntoView({block: 'center'});
                    target.click();
                    return true;
                }"""
            )
            if clicked:
                return "dom-exact"
            raise RuntimeError("no se pudo activar el control exacto 'Load More Games'")

        progress(f"Catálogo Pragmatic dinámico — {self.catalog_url}")
        progress("Control de expansión esperado: texto exacto 'Load More Games'.")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="en-US",
            )
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

                page.wait_for_timeout(1_000)
                initial = ingest(0, page)
                progress(f"Lote inicial: {initial} juegos nuevos; total={len(by_slug)}")

                loads_done = 0
                consecutive_no_growth = 0
                while loads_done < max_loads and not stop_event.is_set():
                    # The control lives at the bottom of the current catalog batch.
                    try:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    except Exception:
                        pass
                    page.wait_for_timeout(250)

                    load_more = exact_load_more(page)
                    if load_more is None:
                        progress("'Load More Games' ya no está visible: catálogo agotado.")
                        break

                    before_visible = game_count(page)
                    before_saved = len(by_slug)
                    loads_done += 1
                    progress(
                        f"Load More Games {loads_done}/{max_loads}: "
                        f"visibles={before_visible}, guardados={before_saved}"
                    )

                    try:
                        strategy = click_load_more(page, load_more)
                    except Exception as exc:
                        progress(f"'Load More Games' no pudo activarse: {type(exc).__name__}: {exc}")
                        break

                    # Do not assume a fixed AJAX latency. The click is considered
                    # successful only when the catalog actually grows, or the
                    # control disappears because the catalog was exhausted.
                    deadline = time.monotonic() + 20.0
                    after_visible = before_visible
                    control_gone = False
                    while time.monotonic() < deadline and not stop_event.is_set():
                        page.wait_for_timeout(250)
                        after_visible = game_count(page)
                        if after_visible > before_visible:
                            break
                        control_gone = exact_load_more(page) is None
                        if control_gone:
                            break

                    new_count = ingest(loads_done, page)
                    progress(
                        f"Carga {loads_done}: estrategia={strategy}, "
                        f"visibles={after_visible}, nuevos={new_count}, total={len(by_slug)}"
                    )

                    if after_visible > before_visible or new_count > 0:
                        consecutive_no_growth = 0
                        continue

                    if control_gone:
                        progress("El control desapareció sin otro lote: catálogo agotado.")
                        break

                    consecutive_no_growth += 1
                    progress(
                        f"El click no agregó juegos (intento sin crecimiento "
                        f"{consecutive_no_growth}/3)."
                    )
                    if consecutive_no_growth >= 3:
                        progress("Tres clicks sin crecimiento; se detiene para evitar un bucle infinito.")
                        break
            finally:
                context.close()
                browser.close()

        games = sorted(by_slug.values(), key=lambda item: item.name.casefold())
        self._write_catalog_index(games)
        progress(
            f"Catálogo Pragmatic terminado: {len(games)} juegos únicos; "
            f"{loads_done} click(s) en 'Load More Games'."
        )
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

        responded = sum(1 for attempt in result.attempts if attempt.ok)
        completed = sum(1 for attempt in result.attempts if attempt.ok and attempt.terminal)
        pending = sum(1 for attempt in result.attempts if attempt.ok and not attempt.terminal)
        failed = sum(1 for attempt in result.attempts if not attempt.ok)
        missing = max(0, result.requested_spins - len(result.attempts))

        # successful_spins now means completed state-machine runs, not merely HTTP
        # responses. Keep actual failures separate from accepted-but-pending states.
        result.successful_spins = completed
        result.failed_spins = failed + missing

        if pending:
            result.status = "PARCIAL"
            continuation_states = sorted(
                {attempt.na for attempt in result.attempts if attempt.ok and not attempt.terminal}
            )
            pending_message = (
                f"{pending} intento(s) respondieron pero no alcanzaron estado terminal; "
                f"estados pendientes={continuation_states}. RAW preservado."
            )
            result.error = f"{result.error}; {pending_message}".strip("; ")

        progress(
            f"Resumen: respondieron={responded}/{result.requested_spins}, "
            f"completados={completed}/{result.requested_spins}, "
            f"pendientes={pending}, errores={failed + missing}"
        )

        if result.run_dir:
            self._write_json(Path(result.run_dir) / "result.json", result.to_dict())
            self._record_last_test_in_game_json(game, result)
        return result
