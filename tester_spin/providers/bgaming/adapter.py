from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from tester_spin.models import Game, utc_now_iso
from tester_spin.providers.base import GameCallback, Progress, ProviderAdapter
from tester_spin.providers.bgaming.catalog import (
    BGamingCatalogRecord,
    filter_records_by_game_type,
    parse_catalog_html,
)
from tester_spin.providers.bgaming.execution import BGamingExecutionMixin


CATALOG_SEARCH_URL = "https://bgaming.com/wp-json/bg/v1/games/search"


def _safe_folder(value: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return clean[:140] or "game"


class BGamingProvider(BGamingExecutionMixin, ProviderAdapter):
    key = "bgaming"
    display_name = "BGaming"
    catalog_url = "https://bgaming.com/game-type/slots"
    min_catalog_reconcile_ratio = 0.70

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.provider_root = data_root / "providers" / self.key
        self.provider_root.mkdir(parents=True, exist_ok=True)
        self.http = self._new_session()

    @staticmethod
    def _new_session() -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            }
        )
        return session

    def game_dir(self, game: Game) -> Path:
        path = self.provider_root / _safe_folder(game.name)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def catalog_record_invalid_reason(self, game: Game) -> str:
        if game.provider != self.key:
            return "provider incorrecto"
        if not game.slug.strip():
            return "slug vacío"
        if not game.name.strip():
            return "nombre vacío"
        if not game.url.strip():
            return "URL vacía"
        return ""

    @staticmethod
    def _record_to_dict(record: BGamingCatalogRecord) -> dict[str, Any]:
        game = record.game
        return {
            "provider": game.provider,
            "slug": game.slug,
            "name": game.name,
            "url": game.url,
            "public_url": record.public_url,
            "demo_url": record.demo_url,
            "thumbnail_url": game.thumbnail_url,
            "thumbnail_path": game.thumbnail_path,
            "identifier": game.symbol,
            "rtp": record.rtp,
            "volatility": record.volatility,
            "game_type": record.game_type,
            "availability": record.availability,
            "catalog_transport": "wordpress_rest_html",
            "runtime_transport": "unknown",
        }

    def _persist_game_catalog_metadata(self, record: BGamingCatalogRecord) -> None:
        path = self.game_dir(record.game) / "game.json"
        current: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current = loaded
            except Exception:
                current = {}
        current.update(self._record_to_dict(record))
        current["catalog_recovery_disabled"] = False
        current["updated_at"] = utc_now_iso()
        path.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _download_thumbnail(self, game: Game, progress: Progress) -> None:
        if not game.thumbnail_url:
            return
        suffix = Path(urlparse(game.thumbnail_url).path).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            suffix = ".img"
        target = self.game_dir(game) / f"thumbnail{suffix}"
        if target.is_file() and target.stat().st_size > 0:
            game.thumbnail_path = str(target)
            return
        try:
            response = self.http.get(game.thumbnail_url, timeout=20.0)
            response.raise_for_status()
            target.write_bytes(response.content)
            game.thumbnail_path = str(target)
        except Exception as exc:
            progress(f"[{game.name}] miniatura BGaming: {type(exc).__name__}: {exc}")

    def _consume_records(
        self,
        records: list[BGamingCatalogRecord],
        *,
        by_slug: dict[str, BGamingCatalogRecord],
        progress: Progress,
        on_game: GameCallback | None,
    ) -> int:
        added = 0
        for record in records:
            slug = record.game.slug
            if slug in by_slug:
                continue
            self._download_thumbnail(record.game, progress)
            self._persist_game_catalog_metadata(record)
            by_slug[slug] = record
            added += 1
            if on_game is not None:
                on_game(record.game)
        return added

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
    ) -> list[Game]:
        limit = max(1, int(max_pages))
        by_slug: dict[str, BGamingCatalogRecord] = {}
        raw_dir = self.provider_root / "catalog-pages"
        raw_dir.mkdir(parents=True, exist_ok=True)
        self.set_catalog_authority(True, "")

        progress(
            "BGaming catálogo: HTML inicial + WordPress REST "
            "/wp-json/bg/v1/games/search; no se usa Playwright."
        )

        response = self.http.get(self.catalog_url, timeout=30.0, allow_redirects=True)
        response.raise_for_status()
        (raw_dir / "page-001.html").write_text(response.text, encoding="utf-8")
        first_records_raw = parse_catalog_html(response.text)
        first_records, first_rejected = filter_records_by_game_type(
            first_records_raw,
            "Slots",
        )
        if not first_records_raw:
            self.set_catalog_authority(
                False,
                "la página inicial no produjo tarjetas [data-catalog-card]",
            )
            raise RuntimeError("BGaming: catálogo inicial vacío/no parseable.")
        if not first_records:
            self.set_catalog_authority(
                False,
                "la página inicial no produjo tarjetas Slots válidas",
            )
            raise RuntimeError("BGaming: catálogo inicial sin juegos tipo Slots.")

        self._consume_records(
            first_records,
            by_slug=by_slug,
            progress=progress,
            on_game=on_game,
        )
        progress(
            f"BGaming catálogo página 1: recibidos={len(first_records_raw)}, "
            f"slots={len(first_records)}, descartados_no_slot={len(first_rejected)}."
        )

        if limit == 1:
            self.set_catalog_authority(False, "crawl limitado manualmente a 1 página")
        else:
            page = 2
            has_more = True
            expected_total: int | None = None

            while has_more and page <= limit and not stop_event.is_set():
                params = {
                    "sort": "release_date",
                    "order": "DESC",
                    "posts_per_page": 25,
                    "format": "html",
                    "columns_style": 1,
                    "game_type": 1,
                    "game_label": 1,
                    "most_popular": 0,
                    "ver": 105,
                    "filter": "game",
                    "page": page,
                    "lang": "en",
                }
                try:
                    api = self.http.get(
                        CATALOG_SEARCH_URL,
                        params=params,
                        timeout=30.0,
                    )
                    api.raise_for_status()
                    payload = api.json()
                    if not isinstance(payload, dict):
                        raise ValueError("respuesta REST no es objeto JSON")
                    (raw_dir / f"page-{page:03d}.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    html = str(payload.get("html") or "")
                    records_raw = parse_catalog_html(html)
                    records, rejected = filter_records_by_game_type(
                        records_raw,
                        "Slots",
                    )
                    reported_page = int(payload.get("page") or page)
                    if reported_page != page:
                        raise ValueError(
                            f"página REST inesperada: pedida={page}, recibida={reported_page}"
                        )

                    try:
                        reported_total = int(payload.get("total") or 0) or None
                    except (TypeError, ValueError):
                        reported_total = None
                    if expected_total is None:
                        expected_total = reported_total
                    elif (
                        reported_total is not None
                        and reported_total != expected_total
                    ):
                        self.set_catalog_authority(
                            False,
                            f"total REST cambió durante el crawl: "
                            f"{expected_total}→{reported_total}",
                        )
                        progress(
                            f"BGaming catálogo inconsistente: total REST cambió "
                            f"{expected_total}→{reported_total} en página {page}."
                        )
                        break

                    added = self._consume_records(
                        records,
                        by_slug=by_slug,
                        progress=progress,
                        on_game=on_game,
                    )
                    has_more = bool(payload.get("hasMore"))
                    progress(
                        f"BGaming catálogo página {page}: recibidos={len(records_raw)}, "
                        f"slots={len(records)}, descartados_no_slot={len(rejected)}, "
                        f"nuevos={added}, acumulados={len(by_slug)}, hasMore={has_more}"
                        + (
                            f", total_reportado={expected_total}"
                            if expected_total is not None
                            else ""
                        )
                    )

                    if has_more and not records_raw:
                        self.set_catalog_authority(
                            False,
                            f"página {page} vacía pero hasMore=true",
                        )
                        break

                    page += 1
                except Exception as exc:
                    self.set_catalog_authority(
                        False,
                        f"falló página REST {page}: {type(exc).__name__}: {exc}",
                    )
                    progress(
                        f"BGaming catálogo PARCIAL en página {page}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    break

            if has_more and page > limit and not stop_event.is_set():
                self.set_catalog_authority(
                    False,
                    f"crawl limitado manualmente a {limit} páginas",
                )

            if (
                not has_more
                and expected_total is not None
                and len(by_slug) != expected_total
            ):
                self.set_catalog_authority(
                    False,
                    "total REST no coincide con slots únicos descubiertos: "
                    f"reportado={expected_total}, descubiertos={len(by_slug)}",
                )
                progress(
                    "BGaming catálogo incompleto: "
                    f"total reportado={expected_total}, "
                    f"slots únicos descubiertos={len(by_slug)}."
                )

        records = sorted(by_slug.values(), key=lambda item: item.game.name.casefold())
        games = [record.game for record in records]
        (self.provider_root / "catalog.json").write_text(
            json.dumps(
                [self._record_to_dict(record) for record in records],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        if stop_event.is_set():
            self.set_catalog_authority(False, "crawl detenido por el usuario")

        progress(
            f"BGaming catálogo terminado: {len(games)} juegos; "
            f"autoridad={'sí' if self.catalog_crawl_authoritative else 'no'}."
        )
        return games

