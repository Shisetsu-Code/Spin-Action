from __future__ import annotations

import json
import threading
from typing import Any

from tester_spin.models import Game
from tester_spin.providers.bgaming.adapter import CATALOG_SEARCH_URL
from tester_spin.providers.bgaming.catalog import BGamingCatalogRecord, parse_catalog_html


PORTFOLIO_URL = "https://bgaming.com/games"


def enumerate_bgaming_portfolio_targets(
    provider: Any,
    *,
    requested_pages: int,
    stop_event: threading.Event,
    progress,
) -> list[Game]:
    """Enumerate BGaming's complete public portfolio without downloading assets.

    The production BGaming crawler intentionally targets Slots. This lab-only
    enumerator starts from /games and omits the REST game_type filter so Cards,
    Casual, Dice, Lottery, Roulette, Scratch and other published types remain in
    the validation inventory.
    """
    pages = int(requested_pages)
    if pages < 0:
        raise ValueError("requested_pages debe ser 0 o positivo")
    limit = 10_000 if pages == 0 else max(1, pages)

    by_slug: dict[str, BGamingCatalogRecord] = {}
    provider.set_catalog_authority(True, "")
    saved_download = getattr(provider, "_download_thumbnail", None)
    if callable(saved_download):
        provider._download_thumbnail = lambda *args, **kwargs: None

    def consume(records: list[BGamingCatalogRecord]) -> int:
        added = 0
        for record in records:
            slug = str(record.game.slug or "").strip()
            if not slug or slug in by_slug:
                continue
            persist = getattr(provider, "_persist_game_catalog_metadata", None)
            if callable(persist):
                persist(record)
            by_slug[slug] = record
            added += 1
        return added

    try:
        progress("BGaming portfolio: /games + REST pagination sin filtro game_type.")
        response = provider.http.get(PORTFOLIO_URL, timeout=30.0, allow_redirects=True)
        response.raise_for_status()
        first = parse_catalog_html(response.text)
        if not first:
            provider.set_catalog_authority(
                False,
                "la página /games no produjo tarjetas [data-catalog-card]",
            )
            raise RuntimeError("BGaming portfolio: página inicial vacía/no parseable.")
        consume(first)
        progress(f"BGaming portfolio página 1: recibidos={len(first)}, acumulados={len(by_slug)}.")

        if limit == 1:
            provider.set_catalog_authority(False, "crawl portfolio limitado manualmente a 1 página")
        else:
            page = 2
            has_more = True
            expected_total_pages: int | None = None
            while has_more and page <= limit and not stop_event.is_set():
                params = {
                    "sort": "release_date",
                    "order": "DESC",
                    "posts_per_page": 25,
                    "format": "html",
                    "columns_style": 1,
                    "game_label": 1,
                    "most_popular": 0,
                    "ver": 105,
                    "filter": "game",
                    "page": page,
                    "lang": "en",
                }
                try:
                    api = provider.http.get(
                        CATALOG_SEARCH_URL,
                        params=params,
                        timeout=30.0,
                    )
                    api.raise_for_status()
                    payload = api.json()
                    if not isinstance(payload, dict):
                        raise ValueError("respuesta REST no es objeto JSON")
                    reported_page = int(payload.get("page") or page)
                    if reported_page != page:
                        raise ValueError(
                            f"página REST inesperada: pedida={page}, recibida={reported_page}"
                        )
                    try:
                        reported_total = int(payload.get("total") or 0) or None
                    except (TypeError, ValueError):
                        reported_total = None
                    if expected_total_pages is None:
                        expected_total_pages = reported_total
                    elif (
                        reported_total is not None
                        and reported_total != expected_total_pages
                    ):
                        provider.set_catalog_authority(
                            False,
                            "total REST cambió durante el portfolio crawl: "
                            f"{expected_total_pages}→{reported_total}",
                        )
                        break

                    records = parse_catalog_html(str(payload.get("html") or ""))
                    added = consume(records)
                    has_more = bool(payload.get("hasMore"))
                    progress(
                        f"BGaming portfolio página {page}: recibidos={len(records)}, "
                        f"nuevos={added}, acumulados={len(by_slug)}, hasMore={has_more}."
                    )
                    if has_more and not records:
                        provider.set_catalog_authority(
                            False,
                            f"página portfolio {page} vacía pero hasMore=true",
                        )
                        break
                    page += 1
                except Exception as exc:
                    provider.set_catalog_authority(
                        False,
                        f"falló página portfolio REST {page}: {type(exc).__name__}: {exc}",
                    )
                    progress(
                        f"BGaming portfolio PARCIAL en página {page}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    break

            if has_more and page > limit and not stop_event.is_set():
                provider.set_catalog_authority(
                    False,
                    f"crawl portfolio limitado manualmente a {limit} páginas",
                )
            if (
                not has_more
                and expected_total_pages is not None
                and (page - 1) != expected_total_pages
            ):
                provider.set_catalog_authority(
                    False,
                    "página terminal portfolio no coincide con total REST: "
                    f"terminal={page - 1}, total_paginas={expected_total_pages}",
                )

        if stop_event.is_set():
            provider.set_catalog_authority(False, "portfolio crawl detenido por el usuario")

        records = sorted(
            by_slug.values(),
            key=lambda item: (
                item.game.slug.casefold(),
                item.game.name.casefold(),
                item.game.url,
            ),
        )
        progress(
            f"BGaming portfolio terminado: {len(records)} juegos; "
            f"autoridad={'sí' if provider.catalog_crawl_authoritative else 'no'}."
        )
        return [record.game for record in records]
    finally:
        if callable(saved_download):
            provider._download_thumbnail = saved_download


__all__ = ["PORTFOLIO_URL", "enumerate_bgaming_portfolio_targets"]
