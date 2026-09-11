from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from tester_spin.models import Game, utc_now_iso
from tester_spin.providers.base import GameCallback, Progress, ProviderAdapter
from tester_spin.providers.redtiger.bootstrap import BootstrapEndpoints
from tester_spin.providers.redtiger.catalog import (
    RedTigerCatalogRecord,
    find_studio_id,
    games_query_params,
    parse_games_page,
)
from tester_spin.providers.redtiger.execution import RedTigerExecutionMixin


DEFAULT_CMS_API = "https://cmsevo.com/api"


def _safe_folder(value: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return clean[:140] or "game"


class RedTigerProvider(RedTigerExecutionMixin, ProviderAdapter):
    key = "redtiger"
    display_name = "Red Tiger"
    catalog_url = "https://redtiger.com/games"
    min_catalog_reconcile_ratio = 0.80
    # The public demo stack was captured as one stateful session. Keep it serial
    # until concurrent independent sessions are explicitly demonstrated.
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
        if not game.symbol.strip():
            return "tableId vacío"
        parsed = urlparse(game.url)
        if not re.fullmatch(r"/games/[^/]+/?", parsed.path or ""):
            return "URL fuera del namespace /games/<slug>"
        return ""

    def table_id_for_game(self, game: Game) -> str:
        table_id = str(game.symbol or "").strip()
        if table_id:
            return table_id
        metadata_path = self.game_dir(game) / "game.json"
        if metadata_path.is_file():
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                payload = None
            if isinstance(payload, dict):
                return str(payload.get("table_id") or "").strip()
        return ""

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
            "cms_id": record.cms_id,
            "table_id": record.table_id,
            "game_type": record.game_type,
            "cms_provider": record.provider_name,
            "release_date": record.release_date,
            "has_bonus_buy": record.has_bonus_buy,
            "catalog_transport": "cmsevo_strapi_json",
            "runtime_transport": "official_launcher_bootstrap_then_http_json",
            "math": attrs.get("math"),
            "information": attrs.get("information"),
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

        progress("Red Tiger catálogo: descubriendo studio CMS por título, sin ID numérico fijo...")
        studios_response = self.http.get(
            f"{self.cms_api_url}/studios",
            params={"populate": "deep"},
            timeout=30.0,
        )
        studios_response.raise_for_status()
        studios_payload = studios_response.json()
        if not isinstance(studios_payload, dict):
            raise ValueError("Red Tiger CMS studios no devolvió objeto JSON.")
        studio_id = find_studio_id(studios_payload, self.display_name)
        (raw_dir / "studios.json").write_text(
            json.dumps(studios_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        progress(f"Red Tiger catálogo: studio resuelto dinámicamente id={studio_id!r}.")

        by_slug: dict[str, RedTigerCatalogRecord] = {}
        released_before = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        page = 1
        page_size = 100
        expected_total: int | None = None
        expected_pages: int | None = None

        while not stop_event.is_set():
            if limit is not None and page > limit:
                if expected_pages is None or page <= expected_pages:
                    authority_gaps.append(f"crawl limitado a {limit} páginas")
                break

            response = self.http.get(
                f"{self.cms_api_url}/games",
                params=games_query_params(
                    studio_id,
                    page=page,
                    page_size=page_size,
                    released_before_iso=released_before,
                ),
                timeout=30.0,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"Red Tiger CMS página {page}: JSON no objeto.")
            (raw_dir / f"page-{page:03d}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            records, pagination = parse_games_page(
                payload,
                public_catalog_url=self.catalog_url,
            )
            data = payload.get("data") if isinstance(payload.get("data"), list) else []
            if len(records) != len(data):
                authority_gaps.append(
                    f"p{page}: registros válidos={len(records)} != data={len(data)}"
                )

            page_total = pagination.get("total")
            page_count = pagination.get("pageCount")
            if page_total is not None:
                if expected_total is None:
                    expected_total = page_total
                elif page_total != expected_total:
                    authority_gaps.append(f"total cambió {expected_total}->{page_total}")
            if page_count is not None:
                if expected_pages is None:
                    expected_pages = page_count
                elif page_count != expected_pages:
                    authority_gaps.append(f"pageCount cambió {expected_pages}->{page_count}")

            added = 0
            for record in records:
                if record.provider_name and record.provider_name.casefold() != self.key:
                    authority_gaps.append(
                        f"{record.game.slug}: cms provider={record.provider_name!r}"
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
            elif len(data) < page_size:
                break
            page += 1

        if stop_event.is_set():
            authority_gaps.append("crawl detenido por el usuario")
        if expected_total is not None and len(by_slug) != expected_total:
            authority_gaps.append(
                f"juegos únicos={len(by_slug)} != total CMS={expected_total}"
            )
        if not by_slug:
            self.set_catalog_authority(False, "CMS no produjo juegos válidos")
            raise RuntimeError("Red Tiger: catálogo CMS vacío o no parseable.")

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
