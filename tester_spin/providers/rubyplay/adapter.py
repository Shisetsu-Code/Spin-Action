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
from tester_spin.providers.rubyplay.catalog import (
    RubyPlayCatalogRecord,
    load_query_payload,
    parse_bricks_catalog_state,
    parse_catalog_html,
    query_loop_html,
    updated_query_meta,
)
from tester_spin.providers.rubyplay.execution import RubyPlayExecutionMixin
from tester_spin.providers.rubyplay.http import mount_rubyplay_system_trust


def _safe_folder(value: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return clean[:140] or "game"


class RubyPlayProvider(RubyPlayExecutionMixin, ProviderAdapter):
    key = "rubyplay"
    display_name = "RubyPlay"
    catalog_url = "https://rubyplay.com/games/"
    min_catalog_reconcile_ratio = 0.80
    # Keep stateful demo sessions serial until RubyPlay's public backend is
    # explicitly validated under concurrent sessions.
    max_test_concurrency = 1

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.provider_root = data_root / "providers" / self.key
        self.provider_root.mkdir(parents=True, exist_ok=True)
        self.http = self._new_session()

    @staticmethod
    def _new_session() -> requests.Session:
        session = requests.Session()
        mount_rubyplay_system_trust(session)
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
        parsed = urlparse(game.url)
        if not re.fullmatch(r"/games/[^/]+/?", parsed.path or ""):
            return "URL fuera del namespace /games/<slug>/"
        return ""

    def _record_to_dict(self, record: RubyPlayCatalogRecord) -> dict[str, Any]:
        game = record.game
        return {
            "provider": game.provider,
            "slug": game.slug,
            "name": game.name,
            "url": game.url,
            "thumbnail_url": game.thumbnail_url,
            "thumbnail_path": game.thumbnail_path,
            "theme": record.theme,
            "catalog_transport": "wordpress_bricks_load_query_page",
            "runtime_transport": "unknown",
        }

    def _persist_game_catalog_metadata(self, record: RubyPlayCatalogRecord) -> None:
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
            progress(f"[{game.name}] miniatura RubyPlay: {type(exc).__name__}: {exc}")

    def _consume_records(
        self,
        records: list[RubyPlayCatalogRecord],
        *,
        by_slug: dict[str, RubyPlayCatalogRecord],
        progress: Progress,
        on_game: GameCallback | None,
    ) -> int:
        added = 0
        for record in records:
            if record.game.slug in by_slug:
                continue
            self._download_thumbnail(record.game, progress)
            self._persist_game_catalog_metadata(record)
            by_slug[record.game.slug] = record
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
        by_slug: dict[str, RubyPlayCatalogRecord] = {}
        raw_dir = self.provider_root / "catalog-pages"
        raw_dir.mkdir(parents=True, exist_ok=True)
        self.set_catalog_authority(True, "")

        progress(
            "RubyPlay catálogo: query Bricks post_type=games + "
            "/wp-json/bricks/v1/load_query_page; sin Playwright ni IDs "
            "de elemento hardcodeados."
        )
        response = self.http.get(
            self.catalog_url,
            timeout=30.0,
            allow_redirects=True,
        )
        response.raise_for_status()
        initial_html = response.text
        (raw_dir / "page-001.html").write_text(initial_html, encoding="utf-8")
        state = parse_bricks_catalog_state(
            initial_html,
            response.url or self.catalog_url,
        )

        selected_html = query_loop_html(initial_html, state.query_element_id)
        if not selected_html:
            if state.candidate_count == 1:
                selected_html = initial_html
            else:
                self.set_catalog_authority(
                    False,
                    "no se pudo aislar el loop Bricks seleccionado entre múltiples candidatos",
                )
                raise RuntimeError(
                    "RubyPlay: se seleccionó el query principal pero no se pudo "
                    "aislar su HTML renderizado."
                )

        records = parse_catalog_html(
            selected_html,
            response.url or self.catalog_url,
        )
        if not records:
            self.set_catalog_authority(
                False,
                "loop principal sin /games/<slug>/ parseables",
            )
            raise RuntimeError("RubyPlay: catálogo inicial vacío/no parseable.")

        self._consume_records(
            records,
            by_slug=by_slug,
            progress=progress,
            on_game=on_game,
        )
        progress(
            f"RubyPlay catálogo: {state.candidate_count} queries post_type=games; "
            f"seleccionado={state.query_element_id} por estructura/capacidad."
        )
        progress(
            f"RubyPlay catálogo página 1: juegos={len(records)}, "
            f"rango={state.start}-{state.end}, max_pages={state.max_pages}."
        )

        expected_pages = state.max_pages
        expected_count: int | None = None
        previous_end = state.end

        if limit < expected_pages:
            self.set_catalog_authority(
                False,
                f"crawl limitado manualmente a {limit} páginas",
            )

        for page in range(2, min(expected_pages, limit) + 1):
            if stop_event.is_set():
                break
            payload = load_query_payload(state, page)
            headers = {}
            if state.wp_rest_nonce:
                headers["X-WP-Nonce"] = state.wp_rest_nonce
            try:
                api = self.http.post(
                    state.load_query_url,
                    params={"lang": state.language},
                    json=payload,
                    headers=headers,
                    timeout=30.0,
                )
                api.raise_for_status()
                data = api.json()
                if not isinstance(data, dict):
                    raise ValueError("respuesta Bricks no es objeto JSON")
                (raw_dir / f"page-{page:03d}.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                meta = updated_query_meta(data)
                if meta["max_pages"] != expected_pages:
                    raise ValueError(
                        f"max_num_pages cambió {expected_pages}->{meta['max_pages']}"
                    )
                if expected_count is None:
                    expected_count = meta["count"]
                elif meta["count"] != expected_count:
                    raise ValueError(
                        f"count cambió {expected_count}->{meta['count']}"
                    )
                if previous_end and meta["start"] != previous_end + 1:
                    raise ValueError(
                        f"rango discontinuo: previo_end={previous_end}, "
                        f"start={meta['start']}"
                    )
                page_records = parse_catalog_html(
                    str(data.get("html") or ""),
                    self.catalog_url,
                )
                expected_page_items = max(0, meta["end"] - meta["start"] + 1)
                if len(page_records) != expected_page_items:
                    raise ValueError(
                        f"página {page}: parseados={len(page_records)}, "
                        f"rango declara={expected_page_items}"
                    )
                added = self._consume_records(
                    page_records,
                    by_slug=by_slug,
                    progress=progress,
                    on_game=on_game,
                )
                previous_end = meta["end"]
                progress(
                    f"RubyPlay catálogo página {page}: "
                    f"recibidos={len(page_records)}, nuevos={added}, "
                    f"acumulados={len(by_slug)}, "
                    f"rango={meta['start']}-{meta['end']}, total={meta['count']}."
                )
            except Exception as exc:
                self.set_catalog_authority(
                    False,
                    f"falló página Bricks {page}: {type(exc).__name__}: {exc}",
                )
                progress(
                    f"RubyPlay catálogo PARCIAL página {page}: "
                    f"{type(exc).__name__}: {exc}"
                )
                break

        if stop_event.is_set():
            self.set_catalog_authority(False, "crawl detenido por el usuario")
        elif self.catalog_crawl_authoritative and limit >= expected_pages:
            if expected_count is not None and len(by_slug) != expected_count:
                self.set_catalog_authority(
                    False,
                    f"count final no coincide: únicos={len(by_slug)}, "
                    f"servidor={expected_count}",
                )
            elif expected_count is None and expected_pages > 1:
                self.set_catalog_authority(
                    False,
                    "no se observó updated_query.count",
                )

        records_out = sorted(
            by_slug.values(),
            key=lambda item: item.game.name.casefold(),
        )
        games = [record.game for record in records_out]
        (self.provider_root / "catalog.json").write_text(
            json.dumps(
                [self._record_to_dict(record) for record in records_out],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        progress(
            f"RubyPlay catálogo terminado: {len(games)} juegos; "
            f"autoridad={'sí' if self.catalog_crawl_authoritative else 'no'}."
        )
        return games
