from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from tester_spin.models import Game, GameTestResult, SpinAttempt, utc_now_iso
from tester_spin.providers.base import GameCallback, Progress, ProviderAdapter
from tester_spin.providers.bgaming.catalog import BGamingCatalogRecord, parse_catalog_html
from tester_spin.providers.bgaming.runtime import (
    balance_total,
    bootstrap_game,
    post_command,
    sanitize_error_text,
    sanitize_options,
    sanitize_session_url,
    validate_init,
    validate_spin,
)


CATALOG_SEARCH_URL = "https://bgaming.com/wp-json/bg/v1/games/search"


def _safe_folder(value: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return clean[:140] or "game"


class BGamingProvider(ProviderAdapter):
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
            "runtime_transport": "http_json_api_v2",
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
        first_records = parse_catalog_html(response.text)
        if not first_records:
            self.set_catalog_authority(
                False,
                "la página inicial no produjo tarjetas [data-catalog-card]",
            )
            raise RuntimeError("BGaming: catálogo inicial vacío/no parseable.")

        self._consume_records(
            first_records,
            by_slug=by_slug,
            progress=progress,
            on_game=on_game,
        )
        progress(f"BGaming catálogo página 1: {len(first_records)} juegos.")

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
                    records = parse_catalog_html(html)
                    reported_page = int(payload.get("page") or page)
                    if reported_page != page:
                        raise ValueError(
                            f"página REST inesperada: pedida={page}, recibida={reported_page}"
                        )

                    if expected_total is None:
                        try:
                            expected_total = int(payload.get("total") or 0) or None
                        except (TypeError, ValueError):
                            expected_total = None

                    added = self._consume_records(
                        records,
                        by_slug=by_slug,
                        progress=progress,
                        on_game=on_game,
                    )
                    has_more = bool(payload.get("hasMore"))
                    progress(
                        f"BGaming catálogo página {page}: recibidos={len(records)}, "
                        f"nuevos={added}, acumulados={len(by_slug)}, hasMore={has_more}"
                        + (
                            f", total_paginas_reportado={expected_total}"
                            if expected_total is not None
                            else ""
                        )
                    )

                    if has_more and not records:
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

    @staticmethod
    def _looks_like_demo_url(url: str) -> bool:
        parsed = urlparse(url)
        return (
            "bgaming-network.com" in parsed.netloc.casefold()
            and ("/play/" in parsed.path or "/games/" in parsed.path)
        )

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        repetitions = max(1, int(spins))
        timeout_s = max(1.0, float(timeout_s))
        started_iso = utc_now_iso()
        started = time.monotonic()
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        run_dir = self.game_dir(game) / "tests" / f"{stamp}-bgaming-http-api-v2"
        attempts: list[SpinAttempt] = []
        errors: list[str] = []
        global_warnings: list[str] = []
        responded = 0
        successes = 0
        discovered_modes: list[dict[str, Any]] = []

        if not self._looks_like_demo_url(game.url):
            elapsed = (time.monotonic() - started) * 1000.0
            return GameTestResult(
                provider=self.key,
                slug=game.slug,
                game_name=game.name,
                game_url=game.url,
                requested_spins=repetitions,
                successful_spins=0,
                failed_spins=repetitions,
                status="PARCIAL",
                symbol=game.symbol,
                started_at=started_iso,
                finished_at=utc_now_iso(),
                elapsed_ms=elapsed,
                error="BGaming: juego catalogado sin Play Demo observado.",
                run_dir=str(run_dir),
            )

        session = self._new_session()
        runtime = None
        init_data: dict[str, Any] = {}
        default_bet: int | float | None = None
        previous_total: int | float | None = None
        expected_reels: int | None = None
        expected_rows: int | None = None

        try:
            progress(
                f"[{game.name}] BGaming: bootstrap HTML → window.__OPTIONS__ → init API v2."
            )
            runtime = bootstrap_game(session, game.url, timeout_s=timeout_s)
            game.symbol = runtime.identifier

            self._write_json(
                run_dir / "bootstrap.json",
                {
                    "identifier": runtime.identifier,
                    "launch_url": sanitize_session_url(runtime.launch_url),
                    "api_url": sanitize_session_url(runtime.api_url),
                    "options": sanitize_options(runtime.options),
                    "round_series_id": runtime.round_series_id,
                },
            )

            _init_response, init_request, init_data = post_command(
                runtime,
                "init",
                timeout_s=timeout_s,
            )
            self._write_json(run_dir / "init-request.json", init_request)
            self._write_json(run_dir / "init-response.json", init_data)

            init_warnings = validate_init(init_data)
            global_warnings.extend(init_warnings)
            options = init_data.get("options")
            if isinstance(options, dict):
                default_bet = options.get("default_bet")
                layout = options.get("layout")
                if isinstance(layout, dict):
                    try:
                        expected_reels = int(layout.get("reels"))
                    except (TypeError, ValueError):
                        expected_reels = None
                    try:
                        expected_rows = int(layout.get("rows"))
                    except (TypeError, ValueError):
                        expected_rows = None

            if not isinstance(default_bet, (int, float)):
                raise ValueError("BGaming init no entregó default_bet utilizable.")

            previous_total = balance_total(init_data)
            flow = init_data.get("flow")
            if isinstance(flow, dict):
                actions = flow.get("available_actions")
                if isinstance(actions, list):
                    for action in actions:
                        action_name = str(action)
                        discovered_modes.append(
                            {
                                "id": action_name.upper(),
                                "kind": "SPIN" if action_name == "spin" else "CONTROL",
                                "observed": action_name in {"init", "spin"},
                            }
                        )

            progress(
                f"[{game.name}] INIT OK: identifier={runtime.identifier}, "
                f"bet={default_bet}, layout={expected_reels or '?'}x{expected_rows or '?'}, "
                f"balance_total={previous_total if previous_total is not None else '—'}"
                + (
                    f", warnings={len(init_warnings)}"
                    if init_warnings
                    else ""
                )
            )
        except Exception as exc:
            message = sanitize_error_text(f"{type(exc).__name__}: {exc}")
            errors.append(message)
            progress(f"[{game.name}] BGaming bootstrap/init ERROR: {message}")

        for number in range(1, repetitions + 1):
            if stop_event.is_set():
                break
            attempt_dir = run_dir / f"attempt-{number:03d}"
            attempt_started = time.monotonic()

            if runtime is None or not isinstance(default_bet, (int, float)):
                message = errors[0] if errors else "BGaming bootstrap/init no disponible."
                attempts.append(
                    SpinAttempt(
                        number=number,
                        ok=False,
                        symbol=game.symbol,
                        error=message,
                        artifact_dir=str(attempt_dir),
                    )
                )
                continue

            try:
                response, request_payload, data = post_command(
                    runtime,
                    "spin",
                    timeout_s=timeout_s,
                    options={"bet": default_bet},
                )
                responded += 1
                self._write_json(attempt_dir / "request.json", request_payload)
                self._write_json(attempt_dir / "response.json", data)

                warnings = validate_spin(
                    data,
                    requested_bet=default_bet,
                    previous_balance_total=previous_total,
                    expected_reels=expected_reels,
                    expected_rows=expected_rows,
                )
                current_total = balance_total(data)
                if current_total is not None:
                    previous_total = current_total

                flow = data.get("flow")
                terminal = (
                    isinstance(flow, dict)
                    and str(flow.get("state") or "") == "closed"
                    and str(flow.get("command") or "") == "spin"
                )
                validated = terminal and not warnings
                successes += int(validated)
                global_warnings.extend(warnings)

                elapsed_ms = (time.monotonic() - attempt_started) * 1000.0
                attempts.append(
                    SpinAttempt(
                        number=number,
                        ok=True,
                        mode_id="SPIN",
                        mode_kind="SPIN",
                        status_code=int(response.status_code),
                        elapsed_ms=elapsed_ms,
                        symbol=runtime.identifier,
                        endpoint=sanitize_session_url(runtime.api_url),
                        na=str(flow.get("state") or "") if isinstance(flow, dict) else "",
                        terminal=terminal,
                        wire_steps=1,
                        warning="; ".join(warnings),
                        artifact_dir=str(attempt_dir),
                    )
                )
                progress(
                    f"[{game.name}] SPIN {number}/{repetitions}: "
                    f"{'OK' if validated else 'PARCIAL'} {elapsed_ms:.0f} ms, "
                    f"balance={current_total if current_total is not None else '—'}"
                    + (f", warnings={len(warnings)}" if warnings else "")
                )
            except Exception as exc:
                elapsed_ms = (time.monotonic() - attempt_started) * 1000.0
                message = f"{type(exc).__name__}: {exc}"
                errors.append(message)
                attempts.append(
                    SpinAttempt(
                        number=number,
                        ok=False,
                        mode_id="SPIN",
                        mode_kind="SPIN",
                        elapsed_ms=elapsed_ms,
                        symbol=game.symbol,
                        endpoint=(
                            sanitize_session_url(runtime.api_url)
                            if runtime is not None
                            else ""
                        ),
                        terminal=False,
                        error=message,
                        artifact_dir=str(attempt_dir),
                    )
                )
                progress(f"[{game.name}] SPIN {number}/{repetitions}: ERROR {message}")

        elapsed_total = (time.monotonic() - started) * 1000.0
        attempted = len(attempts)
        if attempted and successes == attempted and not global_warnings:
            status = "OK"
            error = ""
        elif responded:
            status = "PARCIAL"
            detail: list[str] = [
                f"BGaming respondió {responded}/{attempted}; "
                f"tiradas validadas={successes}/{attempted}."
            ]
            if global_warnings:
                unique = list(dict.fromkeys(global_warnings))
                detail.append("Diagnóstico: " + " | ".join(unique[:5]))
            error = " ".join(detail)
        else:
            status = "ERROR"
            error = errors[0] if errors else "No se completó ninguna tirada BGaming."

        return GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=repetitions,
            successful_spins=successes,
            failed_spins=max(0, attempted - successes),
            status=status,
            symbol=game.symbol,
            discovered_modes=discovered_modes,
            started_at=started_iso,
            finished_at=utc_now_iso(),
            elapsed_ms=elapsed_total,
            error=error,
            run_dir=str(run_dir),
            attempts=attempts,
        )
