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
from tester_spin.providers.rubyplay.browser_catalog import RubyPlayBrowserCatalogClient
from tester_spin.providers.rubyplay.catalog import (
    BricksCatalogState,
    RubyPlayCatalogRecord,
    load_query_payload,
    parse_bricks_catalog_states,
    parse_catalog_html,
    query_loop_html,
    updated_query_element_id,
    updated_query_meta,
)
from tester_spin.providers.rubyplay.execution import RubyPlayExecutionMixin
from tester_spin.providers.rubyplay.http import mount_rubyplay_system_trust


def _safe_folder(value: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return clean[:140] or "game"


def _safe_artifact_component(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")
    return clean[:80] or "query"


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

    def _parse_bricks_page_data(
        self,
        state: BricksCatalogState,
        page: int,
        data: dict[str, Any],
        *,
        artifact_dir: Path,
        source: str,
    ) -> tuple[list[RubyPlayCatalogRecord], dict[str, int]]:
        if not isinstance(data, dict):
            raise ValueError("respuesta Bricks no es objeto JSON")

        artifact_dir.mkdir(parents=True, exist_ok=True)
        suffix = "" if source == "http" else f"-{_safe_artifact_component(source)}"
        (artifact_dir / f"page-{page:03d}{suffix}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        returned_id = updated_query_element_id(data)
        if returned_id and returned_id != state.query_element_id:
            raise ValueError(
                f"updated_query.element_id={returned_id!r} no coincide con "
                f"queryElementId={state.query_element_id!r}"
            )
        meta = updated_query_meta(data)
        records = parse_catalog_html(
            str(data.get("html") or ""),
            self.catalog_url,
        )
        expected_page_items = max(0, meta["end"] - meta["start"] + 1)
        if len(records) != expected_page_items:
            raise ValueError(
                f"página {page}: parseados={len(records)}, "
                f"rango declara={expected_page_items}"
            )
        return records, meta

    def _fetch_bricks_page(
        self,
        state: BricksCatalogState,
        page: int,
        *,
        timeout_s: float,
        artifact_dir: Path,
    ) -> tuple[list[RubyPlayCatalogRecord], dict[str, int]]:
        headers: dict[str, str] = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json; charset=UTF-8",
            "Referer": self.catalog_url,
            "Origin": f"{urlparse(self.catalog_url).scheme}://{urlparse(self.catalog_url).netloc}",
        }
        if state.wp_rest_nonce:
            headers["X-WP-Nonce"] = state.wp_rest_nonce
        api = self.http.post(
            state.load_query_url,
            params={"lang": state.language},
            json=load_query_payload(state, page),
            headers=headers,
            timeout=timeout_s,
        )
        if api.status_code >= 400:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / f"page-{page:03d}-http-{api.status_code}.txt").write_text(
                api.text or "",
                encoding="utf-8",
                errors="replace",
            )
            api.raise_for_status()
        data = api.json()
        return self._parse_bricks_page_data(
            state,
            page,
            data,
            artifact_dir=artifact_dir,
            source="http",
        )

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
    ) -> list[Game]:
        raw_limit = int(max_pages)
        # The active GUI contract is 0=todas. Keep that meaning instead of
        # silently coercing zero to one page.
        limit: int | None = None if raw_limit <= 0 else max(1, raw_limit)
        by_slug: dict[str, RubyPlayCatalogRecord] = {}
        raw_dir = self.provider_root / "catalog-pages"
        raw_dir.mkdir(parents=True, exist_ok=True)
        authority_gaps: list[str] = []
        browser_transport: RubyPlayBrowserCatalogClient | None = None

        progress(
            "RubyPlay catálogo: enumerando todos los query Bricks "
            "post_type=games; cada loop se pagina y el resultado se deduplica "
            "por slug, sin IDs de elemento hardcodeados."
        )
        response = self.http.get(
            self.catalog_url,
            timeout=30.0,
            allow_redirects=True,
        )
        response.raise_for_status()
        initial_html = response.text
        (raw_dir / "initial.html").write_text(initial_html, encoding="utf-8")
        base_url = response.url or self.catalog_url
        states = parse_bricks_catalog_states(initial_html, base_url)
        progress(
            f"RubyPlay catálogo: {len(states)} query loops games descubiertos; "
            "se recorrerán todos."
        )

        for loop_index, state in enumerate(states, start=1):
            if stop_event.is_set():
                authority_gaps.append("crawl detenido por el usuario")
                break

            query_id = state.query_element_id
            query_dir = raw_dir / f"{loop_index:02d}-{_safe_artifact_component(query_id)}"
            loop_slugs: set[str] = set()
            expected_pages = state.max_pages
            expected_count: int | None = None
            previous_end = state.end

            fragment = query_loop_html(initial_html, query_id)
            if not fragment and len(states) == 1:
                fragment = initial_html
            first_records = parse_catalog_html(fragment, base_url) if fragment else []
            expected_first_items = (
                max(0, state.end - state.start + 1)
                if state.end >= state.start
                else 0
            )
            if expected_first_items and len(first_records) != expected_first_items:
                authority_gaps.append(
                    f"{query_id}: página inicial parseada={len(first_records)} "
                    f"pero rango={expected_first_items}"
                )
            if not first_records:
                authority_gaps.append(f"{query_id}: no se pudo aislar/renderizar página 1")
            else:
                loop_slugs.update(record.game.slug for record in first_records)
                added = self._consume_records(
                    first_records,
                    by_slug=by_slug,
                    progress=progress,
                    on_game=on_game,
                )
                (query_dir / "page-001.html").parent.mkdir(parents=True, exist_ok=True)
                (query_dir / "page-001.html").write_text(fragment, encoding="utf-8")
                progress(
                    f"RubyPlay loop {loop_index}/{len(states)} id={query_id} página 1: "
                    f"recibidos={len(first_records)}, nuevos={added}, "
                    f"max_pages={expected_pages}, acumulados={len(by_slug)}."
                )

            if limit is not None and limit < expected_pages:
                authority_gaps.append(
                    f"{query_id}: limitado a {limit}/{expected_pages} páginas"
                )
            target_pages = expected_pages if limit is None else min(expected_pages, limit)

            for page in range(2, target_pages + 1):
                if stop_event.is_set():
                    authority_gaps.append("crawl detenido por el usuario")
                    break
                try:
                    try:
                        page_records, meta = self._fetch_bricks_page(
                            state,
                            page,
                            timeout_s=30.0,
                            artifact_dir=query_dir,
                        )
                    except requests.HTTPError as http_exc:
                        status = (
                            int(http_exc.response.status_code)
                            if http_exc.response is not None
                            else 0
                        )
                        if status != 403:
                            raise
                        body = (
                            str(http_exc.response.text or "")
                            if http_exc.response is not None
                            else ""
                        )
                        excerpt = " ".join(body.split())[:220]
                        progress(
                            f"RubyPlay loop {query_id} página {page}: HTTP 403 en "
                            "replay directo; se reintenta desde el origen del navegador"
                            + (f" ({excerpt})" if excerpt else ".")
                        )
                        if browser_transport is None:
                            browser_transport = RubyPlayBrowserCatalogClient(
                                base_url,
                                timeout_s=30.0,
                            )
                            browser_transport.start()
                            progress(
                                "RubyPlay catálogo: fallback Chromium activo; "
                                "se reutilizará la misma sesión para paginación bloqueada."
                            )
                        browser_response = browser_transport.fetch_page(state, page)
                        page_records, meta = self._parse_bricks_page_data(
                            state,
                            page,
                            browser_response.data,
                            artifact_dir=query_dir,
                            source="browser",
                        )

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
                    previous_end = meta["end"]
                    loop_slugs.update(record.game.slug for record in page_records)
                    added = self._consume_records(
                        page_records,
                        by_slug=by_slug,
                        progress=progress,
                        on_game=on_game,
                    )
                    progress(
                        f"RubyPlay loop {loop_index}/{len(states)} id={query_id} "
                        f"página {page}: recibidos={len(page_records)}, nuevos={added}, "
                        f"loop={len(loop_slugs)}/{meta['count']}, "
                        f"acumulados={len(by_slug)}."
                    )
                except Exception as exc:
                    authority_gaps.append(
                        f"{query_id} p{page}: {type(exc).__name__}: {exc}"
                    )
                    progress(
                        f"RubyPlay loop {query_id} PARCIAL página {page}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    break

            loop_complete = not stop_event.is_set() and target_pages >= expected_pages
            if loop_complete:
                if expected_count is not None and len(loop_slugs) != expected_count:
                    authority_gaps.append(
                        f"{query_id}: únicos={len(loop_slugs)} != count={expected_count}"
                    )
                elif expected_count is None and expected_pages == 1:
                    # For a one-page loop, Bricks' initial start/end range is
                    # sufficient to validate the rendered number of posts.
                    expected_single = expected_first_items
                    if expected_single and len(loop_slugs) != expected_single:
                        authority_gaps.append(
                            f"{query_id}: únicos={len(loop_slugs)} != rango={expected_single}"
                        )

        if browser_transport is not None:
            browser_transport.close()

        if not by_slug:
            self.set_catalog_authority(False, "ningún loop produjo juegos parseables")
            raise RuntimeError("RubyPlay: ningún query Bricks produjo juegos parseables.")

        if authority_gaps:
            detail = "; ".join(authority_gaps[:4])
            if len(authority_gaps) > 4:
                detail += f"; +{len(authority_gaps) - 4} incidencias"
            self.set_catalog_authority(False, detail)
        else:
            self.set_catalog_authority(True, "")

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
            f"RubyPlay catálogo terminado: {len(games)} juegos únicos desde "
            f"{len(states)} loops; autoridad="
            f"{'sí' if self.catalog_crawl_authoritative else 'no'}."
        )
        if authority_gaps:
            progress(
                "RubyPlay catálogo incidencias de autoridad: "
                + "; ".join(authority_gaps[:6])
            )
        return games
