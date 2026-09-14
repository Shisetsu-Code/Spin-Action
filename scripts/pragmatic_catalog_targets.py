from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from tester_spin.models import Game
from tester_spin.providers.pragmatic_catalog_ajax import (
    AjaxPage,
    _fetch_ajax_page,
    _items_per_page,
)


Progress = Callable[[str], None]
FetchAjax = Callable[[Any, int], AjaxPage]


def _set_authority(provider: Any, authoritative: bool, reason: str) -> None:
    setter = getattr(provider, "set_catalog_authority", None)
    if callable(setter):
        setter(bool(authoritative), str(reason))
        return
    provider.catalog_crawl_authoritative = bool(authoritative)
    provider.catalog_crawl_reason = str(reason)


def enumerate_pragmatic_targets(
    provider,
    *,
    stop_event: threading.Event,
    progress: Progress,
    max_pages: int = 0,
    items_per_page_override: int | None = None,
    fetch_ajax: FetchAjax = _fetch_ajax_page,
) -> list[Game]:
    """Enumerate Pragmatic game targets without persisting thumbnails/metadata.

    This lab-only enumerator deliberately performs only the catalog requests needed
    to identify game URLs. It does not call ``_persist_catalog_artifacts`` and it
    requests AJAX pages sequentially so it never over-fetches pages beyond the
    first official short AJAX page.

    The initial HTML page is not authoritative for catalog termination. Pragmatic's
    production crawler validates AJAX page 2 even when the initial page contains
    fewer cards than ``items-per-page``; the lab enumerator mirrors that behavior.

    ``max_pages=0`` means continue until the official terminal AJAX page. A positive
    value is a hard total-page cap and is useful only for sampling. Catalog authority
    is granted only after observing a short AJAX page; limits, cancellation and
    transport errors remain fail-closed/non-authoritative.
    """
    page_cap = max(0, int(max_pages))
    _set_authority(provider, False, "enumeración Pragmatic de laboratorio aún no cerrada")

    response = provider.http.get(provider.catalog_url, timeout=30.0)
    response.raise_for_status()

    per_page = (
        max(1, int(items_per_page_override))
        if items_per_page_override is not None
        else _items_per_page(response.text)
    )
    by_slug: dict[str, Game] = {}

    def ingest(rows: list[Game]) -> None:
        for game in rows:
            if game.slug and game.slug not in by_slug:
                by_slug[game.slug] = game

    initial = provider._extract_catalog_page(response.text, response.url)
    ingest(initial)
    progress(
        f"Pragmatic targets: página 1 juegos={len(initial)}, total={len(by_slug)}, "
        f"sin descargar miniaturas."
    )

    if stop_event.is_set():
        _set_authority(provider, False, "enumeración Pragmatic cancelada antes de validar AJAX")
        return sorted(by_slug.values(), key=lambda game: (game.slug.casefold(), game.name.casefold()))
    if page_cap == 1:
        _set_authority(provider, False, "límite manual de catálogo Pragmatic alcanzado en página 1")
        return sorted(by_slug.values(), key=lambda game: (game.slug.casefold(), game.name.casefold()))

    page = 2
    while not stop_event.is_set():
        if page_cap and page > page_cap:
            _set_authority(
                provider,
                False,
                f"límite manual de catálogo Pragmatic alcanzado antes de página terminal (cap={page_cap})",
            )
            break

        item = fetch_ajax(provider, page)
        if item.status != 200 or item.error:
            reason = item.error or f"HTTP {item.status}"
            _set_authority(provider, False, f"AJAX página {page} falló: {reason}")
            raise RuntimeError(
                f"Pragmatic target enumeration failed on page {page}: {reason}"
            )

        ingest(item.games)
        progress(
            f"Pragmatic targets: página {page} juegos={len(item.games)}, "
            f"total={len(by_slug)}, sin persistencia."
        )
        if len(item.games) < per_page:
            _set_authority(
                provider,
                True,
                f"Pragmatic AJAX cerrado por página terminal {page}: {len(item.games)} < {per_page}",
            )
            break
        page += 1

    if stop_event.is_set():
        _set_authority(provider, False, "enumeración Pragmatic cancelada antes de página terminal")

    return sorted(by_slug.values(), key=lambda game: (game.slug.casefold(), game.name.casefold()))


__all__ = ["enumerate_pragmatic_targets"]
