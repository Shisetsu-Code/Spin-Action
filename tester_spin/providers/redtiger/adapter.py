from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from tester_spin.models import Game, utc_now_iso
from tester_spin.providers.base import GameCallback, Progress, ProviderAdapter
from tester_spin.providers.redtiger.bootstrap import BootstrapEndpoints
from tester_spin.providers.redtiger.catalog import (
    RedTigerCatalogRecord,
    provider_id_from_catalog_url,
    parse_wp_games_page,
    wp_catalog_query_params,
)
from tester_spin.providers.redtiger.cms_auth import discover_cms_authorization
from tester_spin.providers.redtiger.execution import RedTigerExecutionMixin


DEFAULT_CMS_API = "https://cmsevo.com/api"


def _safe_folder(value: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return clean[:140] or "game"


def _normalized_provider(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


class RedTigerProvider(RedTigerExecutionMixin, ProviderAdapter):
    key = "redtiger"
    display_name = "Red Tiger"
    catalog_url = (
        "https://games.evolution.com/all-games/"
        "?game_provider%5B0%5D=1185&custom_sort=featured"
    )
    min_catalog_reconcile_ratio = 0.80
    max_test_concurrency = 1

    def __init__(
        self,
        data_root: Path,
        *,
        cms_api_url: str = DEFAULT_CMS_API,
        bootstrap_endpoints: BootstrapEndpoints | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.provider_root = self.data_root / "providers" / self.key
        self.provider_root.mkdir(parents=True, exist_ok=True)
        # Retained only for historical cmsevo helpers/tests. Active catalog uses
        # the public Evolution Games WordPress endpoint derived from catalog_url.
        self.cms_api_url = str(cms_api_url).rstrip("/")
        self.bootstrap_endpoints = bootstrap_endpoints or BootstrapEndpoints()
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
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            }
        )
        return session

    def _cms_get(
        self,
        path: str,
        *,
        params: dict[str, Any],
        timeout_s: float,
        progress: Progress,
    ) -> requests.Response:
        """Legacy cmsevo helper retained for historical artifacts/tests only."""
        parsed_public = urlparse(self.catalog_url)
        origin = f"{parsed_public.scheme}://{parsed_public.netloc}"
        headers = {"Origin": origin, "Referer": origin.rstrip("/") + "/"}
        url = f"{self.cms_api_url}/{str(path).lstrip('/')}"
        response = self.http.get(url, params=params, headers=headers, timeout=timeout_s)
        if response.status_code not in {401, 403}:
            response.raise_for_status()
            return response

        progress(
            f"Red Tiger CMS {response.status_code}: obteniendo autorización "
            "desde una request real del frontend oficial..."
        )
        authorization = discover_cms_authorization(
            self.catalog_url,
            timeout_s=max(30.0, float(timeout_s)),
        )
        self.http.headers["Authorization"] = authorization
        response = self.http.get(url, params=params, headers=headers, timeout=timeout_s)
        response.raise_for_status()
        progress("Red Tiger CMS: autorización del frontend reutilizada en memoria; replay HTTP OK.")
        return response

    def _wp_catalog_get(self, *, params: dict[str, Any], timeout_s: float) -> requests.Response:
        parsed = urlparse(self.catalog_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        url = origin.rstrip("/") + "/wp-json/wp/v2/pages"
        response = self.http.get(
            url,
            params=params,
            headers={"Referer": self.catalog_url, "Accept": "application/json, text/plain, */*"},
            timeout=timeout_s,
        )
        response.raise_for_status()
        return response

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
        if not str(game.symbol or "").strip():
            return "Evolution post id vacío"
        parsed = urlparse(game.url)
        if parsed.netloc.casefold() != "games.evolution.com":
            return "host fuera de games.evolution.com"
        if not re.fullmatch(r"/slots/[^/]+/?", parsed.path or ""):
            return "URL fuera del namespace /slots/<slug>/"
        return ""

    def launch_id_for_game(self, game: Game) -> str:
        launch_id = str(game.symbol or "").strip()
        if launch_id:
            return launch_id
        metadata_path = self.game_dir(game) / "game.json"
        if metadata_path.is_file():
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                payload = None
            if isinstance(payload, dict):
                return str(payload.get("launch_id") or payload.get("wp_post_id") or "").strip()
        return ""

    def table_id_for_game(self, game: Game) -> str:
        """Compatibility accessor for old callers/artifacts.

        The new public flow launches by WordPress post id, not by acf.game_id.
        """
        return self.launch_id_for_game(game)

    def _record_dict(self, record: RedTigerCatalogRecord) -> dict[str, Any]:
        game = record.game
        attrs = record.raw_attributes
        return {
            "provider": self.key,
            "slug": game.slug,
            "name": game.name,
            "url": game.url,
            "thumbnail_url": game.thumbnail_url,
            "thumbnail_path": game.thumbnail_path,
            "wp_post_id": record.cms_id,
            "launch_id": record.launch_id or str(record.cms_id or ""),
            "catalog_game_id": record.table_id,
            "game_type": record.game_type,
            "catalog_provider": record.provider_name,
            "release_date": record.release_date,
            "has_bonus_buy": record.has_bonus_buy,
            "catalog_transport": "games_evolution_wordpress_json",
            "runtime_transport": "games_evolution_start_iframe_then_http_json",
            "rtp": attrs.get("rtp"),
            "volatility": attrs.get("volatility"),
            "release_year": attrs.get("release_year"),
        }

    def _persist_record(self, record: RedTigerCatalogRecord) -> None:
        path = self.game_dir(record.game) / "game.json"
        current: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current = loaded
            except Exception:
                current = {}
        current.update(self._record_dict(record))
        current["catalog_recovery_disabled"] = False
        current["updated_at"] = utc_now_iso()
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

    def _download_thumbnail(self, game: Game, progress: Progress) -> None:
        if not game.thumbnail_url:
            return
        suffix = Path(urlparse(game.thumbnail_url).path).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}:
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
            progress(f"[{game.name}] miniatura Red Tiger: {type(exc).__name__}: {exc}")

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
    ) -> list[Game]:
        limit = None if int(max_pages) <= 0 else max(1, int(max_pages))
        raw_dir = self.provider_root / "catalog-pages"
        raw_dir.mkdir(parents=True, exist_ok=True)
        authority_gaps: list[str] = []

        provider_id = provider_id_from_catalog_url(self.catalog_url)
        parsed_catalog_query = parse_qs(urlparse(self.catalog_url).query)
        custom_sort = str((parsed_catalog_query.get("custom_sort") or ["featured"])[0] or "featured")
        progress(
            "Red Tiger catálogo: usando games.evolution.com WordPress; "
            f"provider={provider_id}, orden={custom_sort}."
        )

        by_slug: dict[str, RedTigerCatalogRecord] = {}
        page = 1
        page_size = 36
        expected_total: int | None = None
        expected_pages: int | None = None

        while not stop_event.is_set():
            if limit is not None and page > limit:
                if expected_pages is None or page <= expected_pages:
                    authority_gaps.append(f"crawl limitado a {limit} páginas")
                break

            response = self._wp_catalog_get(
                params=wp_catalog_query_params(
                    provider_id,
                    page=page,
                    page_size=page_size,
                    custom_sort=custom_sort,
                ),
                timeout_s=30.0,
            )
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError(f"Evolution Games página {page}: JSON no lista.")
            (raw_dir / f"page-{page:03d}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            records = parse_wp_games_page(payload)
            if len(records) != len(payload):
                authority_gaps.append(
                    f"p{page}: registros válidos={len(records)} != data={len(payload)}"
                )

            try:
                page_total = int(response.headers.get("X-WP-Total", "") or 0) or None
            except (TypeError, ValueError):
                page_total = None
            try:
                page_count = int(response.headers.get("X-WP-TotalPages", "") or 0) or None
            except (TypeError, ValueError):
                page_count = None

            if page_total is not None:
                if expected_total is None:
                    expected_total = page_total
                elif page_total != expected_total:
                    authority_gaps.append(f"total cambió {expected_total}->{page_total}")
            if page_count is not None:
                if expected_pages is None:
                    expected_pages = page_count
                elif page_count != expected_pages:
                    authority_gaps.append(f"totalPages cambió {expected_pages}->{page_count}")

            added = 0
            for record in records:
                if record.provider_name and _normalized_provider(record.provider_name) != _normalized_provider(self.display_name):
                    authority_gaps.append(
                        f"{record.game.slug}: provider={record.provider_name!r}"
                    )
                    continue
                if record.game.slug in by_slug:
                    continue
                self._download_thumbnail(record.game, progress)
                self._persist_record(record)
                by_slug[record.game.slug] = record
                added += 1
                if on_game is not None:
                    on_game(record.game)

            progress(
                f"Red Tiger catálogo página {page}: recibidos={len(records)}, "
                f"nuevos={added}, acumulados={len(by_slug)}, total={expected_total or '—'}."
            )

            if expected_pages is not None:
                if page >= expected_pages:
                    break
            elif len(payload) < page_size:
                break
            else:
                # The HAR exposes X-WP-TotalPages. Without it we can continue, but
                # the crawl is not authoritative enough for destructive reconcile.
                if page == 1:
                    authority_gaps.append("X-WP-TotalPages ausente")
            page += 1

        if stop_event.is_set():
            authority_gaps.append("crawl detenido por el usuario")
        if expected_total is not None and len(by_slug) != expected_total:
            authority_gaps.append(
                f"juegos únicos={len(by_slug)} != total WordPress={expected_total}"
            )
        if not by_slug:
            self.set_catalog_authority(False, "WordPress no produjo juegos válidos")
            raise RuntimeError("Red Tiger: catálogo Evolution Games vacío o no parseable.")

        self.set_catalog_authority(not authority_gaps, "; ".join(authority_gaps[:6]))
        records_out = sorted(by_slug.values(), key=lambda item: item.game.name.casefold())
        games = [record.game for record in records_out]
        (self.provider_root / "catalog.json").write_text(
            json.dumps([self._record_dict(record) for record in records_out], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        progress(
            f"Red Tiger catálogo terminado: {len(games)} juegos; "
            f"autoridad={'sí' if self.catalog_crawl_authoritative else 'no'}."
        )
        if authority_gaps:
            progress("Red Tiger catálogo incidencias: " + "; ".join(authority_gaps[:6]))
        return games
