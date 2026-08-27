from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from tester_spin.models import Game, utc_now_iso
from tester_spin.providers.base import GameCallback, Progress
from tester_spin.providers.pragmatic import _human_from_slug
from tester_spin.providers.pragmatic_catalog_preloaded import crawl_pragmatic_catalog_preloaded


DEFAULT_ITEMS_PER_PAGE = 9
PAGE_BATCH_SIZE = 8
AJAX_TIMEOUT_S = 20.0
_AJAX_LOCAL = threading.local()
_PERSIST_LOCAL = threading.local()


@dataclass(slots=True)
class AjaxPage:
    page: int
    url: str
    status: int
    elapsed_ms: float
    games: list[Game]
    error: str = ""


def _items_per_page(html: str) -> int:
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        node = soup.select_one('input[name="items-per-page"]')
        if node is not None:
            value = int(str(node.get("value") or "").strip())
            if 1 <= value <= 100:
                return value
    except Exception:
        pass
    return DEFAULT_ITEMS_PER_PAGE


def _ajax_url(base_url: str, page: int) -> str:
    # Exact request shape observed in the full Firefox HAR and in Pragmatic's
    # gamesFilters JavaScript C() builder on 2026-08-27.
    params = [
        ("ajax", "1"),
        ("cats", "undefined"),
        ("lang", "en"),
        ("cur", "USD"),
        ("studio", "all"),
        ("device", "undefined"),
        ("search", ""),
        ("page", str(int(page))),
    ]
    separator = "&" if "?" in base_url else "?"
    return base_url.rstrip("?") + separator + urlencode(params)


def _worker_session(provider) -> requests.Session:
    session = getattr(_AJAX_LOCAL, "session", None)
    if session is not None:
        return session

    session = requests.Session()
    session.headers.update(dict(provider.http.headers))
    session.headers.update(
        {
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": provider.catalog_url,
        }
    )
    try:
        session.cookies.update(provider.http.cookies.get_dict())
    except Exception:
        pass
    _AJAX_LOCAL.session = session
    return session


def _fetch_ajax_page(provider, page_no: int) -> AjaxPage:
    url = _ajax_url(provider.catalog_url, page_no)
    last_error = ""
    for attempt in range(1, 4):
        started = time.monotonic()
        try:
            response = _worker_session(provider).get(url, timeout=AJAX_TIMEOUT_S)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                if response.status_code in {403, 429, 500, 502, 503, 504} and attempt < 3:
                    time.sleep(0.15 * attempt)
                    continue
                return AjaxPage(page_no, url, response.status_code, elapsed_ms, [], last_error)

            games = provider._extract_catalog_page(response.text, response.url)
            return AjaxPage(page_no, response.url, response.status_code, elapsed_ms, games)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 3:
                time.sleep(0.15 * attempt)

    return AjaxPage(page_no, url, 0, 0.0, [], last_error or "request failed")


def _persist(provider, game: Game, source: str) -> tuple[Game, str]:
    try:
        worker_provider = getattr(_PERSIST_LOCAL, "provider", None)
        if worker_provider is None:
            worker_provider = provider.__class__(provider.data_root, base_bet=provider.base_bet)
            worker_provider.catalog_url = provider.catalog_url
            _PERSIST_LOCAL.provider = worker_provider
        worker_provider._persist_catalog_artifacts(game, source)
        return game, ""
    except Exception as exc:
        return game, f"{type(exc).__name__}: {exc}"


def _looks_like_ajax_catalog(page: AjaxPage) -> bool:
    if page.status != 200 or page.error:
        return False
    # A valid terminal page is allowed to contain 0..N games. For the first AJAX
    # page we require at least one game so a WAF/error HTML page cannot be mistaken
    # for the end of the catalog.
    return bool(page.games)


def crawl_pragmatic_catalog_ajax(
    provider,
    *,
    stop_event: threading.Event,
    progress: Progress,
    max_pages: int,
    on_game: GameCallback | None = None,
) -> list[Game]:
    """Enumerate Pragmatic games via the exact Load More AJAX endpoint.

    Full HAR evidence shows the official frontend increments ``page`` and requests:

      /en/games/?ajax=1&cats=undefined&lang=en&cur=USD&studio=all
      &device=undefined&search=&page=N

    Each normal page contains nine ``.game__thumbnail`` anchors. The official JS
    hides Load More when the returned thumbnail count is smaller than the hidden
    ``items-per-page`` value (currently 9). Pages are independent GETs, so this
    crawler requests small consecutive batches in parallel and commits results in
    page order, stopping at the first short page.
    """
    max_loads = max(1, int(max_pages))
    by_slug: dict[str, Game] = {}
    source_by_slug: dict[str, str] = {}
    diagnostics: list[dict[str, Any]] = []
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    diag_root = provider.provider_root / "catalog-diagnostics" / f"{stamp}-ajax-direct"
    diag_root.mkdir(parents=True, exist_ok=True)

    def ingest(games: list[Game], source: str) -> int:
        added = 0
        for game in games:
            current = by_slug.get(game.slug)
            if current is not None:
                if not current.thumbnail_url and game.thumbnail_url:
                    current.thumbnail_url = game.thumbnail_url
                if current.name == _human_from_slug(game.slug) and game.name != current.name:
                    current.name = game.name
                continue
            by_slug[game.slug] = game
            source_by_slug[game.slug] = source
            added += 1
            if on_game is not None:
                on_game(game)
        return added

    progress("Catálogo Pragmatic AJAX DIRECT: usando el endpoint real de Load More del HAR.")

    try:
        initial_response = provider.http.get(provider.catalog_url, timeout=30.0)
        initial_response.raise_for_status()
    except Exception as exc:
        progress(f"No se pudo abrir el catálogo HTTP: {type(exc).__name__}: {exc}; fallback DOM.")
        return crawl_pragmatic_catalog_preloaded(
            provider,
            stop_event=stop_event,
            progress=progress,
            max_pages=max_pages,
            on_game=on_game,
        )

    per_page = _items_per_page(initial_response.text)
    initial_games = provider._extract_catalog_page(initial_response.text, initial_response.url)
    ingest(initial_games, initial_response.url)
    progress(
        f"Página 1: juegos={len(initial_games)}, items-per-page={per_page}, total={len(by_slug)}"
    )

    if stop_event.is_set():
        games = sorted(by_slug.values(), key=lambda item: item.name.casefold())
        provider._write_catalog_index(games)
        return games

    # Validate the HAR-derived endpoint before launching broad parallel batches.
    first_ajax = _fetch_ajax_page(provider, 2)
    diagnostics.append(
        {
            "page": 2,
            "url": first_ajax.url,
            "status": first_ajax.status,
            "elapsed_ms": round(first_ajax.elapsed_ms, 2),
            "games": len(first_ajax.games),
            "error": first_ajax.error,
        }
    )
    if not _looks_like_ajax_catalog(first_ajax):
        progress(
            "El endpoint AJAX no devolvió una página de juegos válida; "
            "se usa el crawler DOM como fallback."
        )
        provider._write_json(diag_root / "ajax-validation-failed.json", diagnostics[-1])
        return crawl_pragmatic_catalog_preloaded(
            provider,
            stop_event=stop_event,
            progress=progress,
            max_pages=max_pages,
            on_game=on_game,
        )

    added = ingest(first_ajax.games, first_ajax.url)
    progress(
        f"AJAX página 2: juegos={len(first_ajax.games)}, nuevos={added}, "
        f"{first_ajax.elapsed_ms:.0f} ms, total={len(by_slug)}"
    )

    terminal_page: int | None = 2 if len(first_ajax.games) < per_page else None
    pages_requested = 1
    next_page = 3
    max_ajax_page = max_loads + 1

    while terminal_page is None and next_page <= max_ajax_page and not stop_event.is_set():
        batch = list(range(next_page, min(next_page + PAGE_BATCH_SIZE, max_ajax_page + 1)))
        results: dict[int, AjaxPage] = {}

        with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="catalog-ajax") as pool:
            futures = {pool.submit(_fetch_ajax_page, provider, page_no): page_no for page_no in batch}
            for future in as_completed(futures):
                page_no = futures[future]
                try:
                    results[page_no] = future.result()
                except Exception as exc:
                    results[page_no] = AjaxPage(
                        page_no,
                        _ajax_url(provider.catalog_url, page_no),
                        0,
                        0.0,
                        [],
                        f"{type(exc).__name__}: {exc}",
                    )

        for page_no in batch:
            page = results[page_no]
            pages_requested += 1
            diagnostics.append(
                {
                    "page": page_no,
                    "url": page.url,
                    "status": page.status,
                    "elapsed_ms": round(page.elapsed_ms, 2),
                    "games": len(page.games),
                    "error": page.error,
                }
            )

            if page.error or page.status != 200:
                progress(
                    f"AJAX página {page_no} falló ({page.error or page.status}); "
                    "fallback DOM para no truncar el catálogo."
                )
                provider._write_json(diag_root / "ajax-pages.json", diagnostics)
                return crawl_pragmatic_catalog_preloaded(
                    provider,
                    stop_event=stop_event,
                    progress=progress,
                    max_pages=max_pages,
                    on_game=on_game,
                )

            added = ingest(page.games, page.url)
            progress(
                f"AJAX página {page_no}: juegos={len(page.games)}, nuevos={added}, "
                f"{page.elapsed_ms:.0f} ms, total={len(by_slug)}"
            )

            if len(page.games) < per_page:
                terminal_page = page_no
                progress(
                    f"Fin oficial del catálogo: página {page_no} trae "
                    f"{len(page.games)} < {per_page} juegos."
                )
                break

        next_page += PAGE_BATCH_SIZE

    games = sorted(by_slug.values(), key=lambda item: item.name.casefold())
    provider._write_catalog_index(games)
    provider._write_json(
        diag_root / "ajax-pages.json",
        {
            "schema": "tester-spin/pragmatic-ajax-catalog/v1",
            "updated_at": utc_now_iso(),
            "items_per_page": per_page,
            "batch_size": PAGE_BATCH_SIZE,
            "terminal_page": terminal_page,
            "pages_requested": pages_requested,
            "game_count": len(games),
            "pages": diagnostics,
        },
    )

    # Keep discovery fast: only after the whole list is known do we download native
    # thumbnails and write per-game metadata, in parallel. Games were already emitted
    # to the GUI through on_game above.
    if games and not stop_event.is_set():
        workers = min(10, max(4, len(games) // 30 + 1))
        progress(
            f"Enumeración AJAX lista: {len(games)} juegos. "
            f"Guardando miniaturas/metadata en paralelo ({workers} workers)."
        )
        done = 0
        errors = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="catalog-artifact") as pool:
            futures = [
                pool.submit(_persist, provider, game, source_by_slug.get(game.slug, provider.catalog_url))
                for game in games
            ]
            for future in as_completed(futures):
                if stop_event.is_set():
                    for pending in futures:
                        pending.cancel()
                    break
                game, error = future.result()
                done += 1
                if error:
                    errors += 1
                    progress(f"[{game.name}] miniatura/metadata: {error}")
                if done % 25 == 0 or done == len(games):
                    progress(f"Miniaturas/metadata: {done}/{len(games)}; errores={errors}")

    provider._write_catalog_index(games)
    progress(
        f"Catálogo AJAX terminado: {len(games)} juegos; "
        f"página final={terminal_page if terminal_page is not None else 'límite configurado'}."
    )
    return games
