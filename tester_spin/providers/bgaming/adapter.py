from __future__ import annotations

import json
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from tester_spin.models import Game, GameTestResult, SpinAttempt, utc_now_iso
from tester_spin.providers.base import GameCallback, Progress, ProviderAdapter
from tester_spin.providers.bgaming.catalog import (
    BGamingCatalogRecord,
    filter_records_by_game_type,
    parse_catalog_html,
)
from tester_spin.providers.bgaming.hyperhive import (
    is_hyperhive_runtime,
    run_hyperhive_test,
)
from tester_spin.providers.bgaming.runtime import (
    balance_total,
    bootstrap_game,
    build_line_bets,
    discover_purchase_modes,
    flow_continuation_command,
    is_line_bet_init,
    is_switchable_container_init,
    line_bet_count,
    pending_flow_actions,
    post_command,
    preselection_multiplier,
    purchase_expected_debit,
    purchase_names_equivalent,
    resolve_base_bet,
    sanitize_error_text,
    sanitize_options,
    sanitize_session_url,
    spin_remote_proof,
    validate_init,
    validate_line_spin,
    validate_spin,
)
from tester_spin.providers.bgaming.switchable import (
    run_switchable_container_test,
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
                        f"BGaming catálogo página {page}: recibidos={len(records_raw)}, "
                        f"slots={len(records)}, descartados_no_slot={len(rejected)}, "
                        f"nuevos={added}, acumulados={len(by_slug)}, hasMore={has_more}"
                        + (
                            f", total_paginas_reportado={expected_total}"
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
        responded_attempts = 0
        successes = 0
        discovered_modes: list[dict[str, Any]] = []
        discovered_mode_ids: set[str] = set()
        pending_actions: set[str] = set()
        previous_remote_identity: tuple[Any, Any, str] | None = None

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
        variable_layout = False
        legacy_line_bets = False
        legacy_line_count = 0
        rows_required = False
        purchase_modes: list[dict[str, Any]] = []
        mode_specs: list[dict[str, Any]] = [
            {"id": "SPIN", "kind": "SPIN", "purchase": None}
        ]

        def register_mode(mode: dict[str, Any]) -> None:
            mode_id = str(mode.get("id") or "")
            if not mode_id or mode_id in discovered_mode_ids:
                return
            discovered_modes.append(mode)
            discovered_mode_ids.add(mode_id)

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

            if is_hyperhive_runtime(runtime):
                progress(
                    f"[{game.name}] runtime HyperHive detectado; "
                    "cambiando a JSON-RPC /api."
                )
                return run_hyperhive_test(
                    game=game,
                    runtime=runtime,
                    spins=repetitions,
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=progress,
                    run_dir=run_dir,
                    started_iso=started_iso,
                    started_monotonic=started,
                )

            _init_response, init_request, init_data = post_command(
                runtime,
                "init",
                timeout_s=timeout_s,
            )
            self._write_json(run_dir / "init-request.json", init_request)
            self._write_json(run_dir / "init-response.json", init_data)

            if is_switchable_container_init(init_data):
                progress(
                    f"[{game.name}] init de contenedor detectado; "
                    "descubriendo variantes apostables."
                )
                return run_switchable_container_test(
                    game=game,
                    runtime=runtime,
                    initial_data=init_data,
                    spins=repetitions,
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=progress,
                    run_dir=run_dir,
                    started_iso=started_iso,
                    started_monotonic=started,
                )

            init_warnings = validate_init(init_data)
            global_warnings.extend(init_warnings)
            options = init_data.get("options")
            bet_source = ""
            if isinstance(options, dict):
                default_bet, bet_source = resolve_base_bet(init_data)
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

            name_lower = game.name.casefold()
            variable_layout = (
                (expected_rows or 0) >= 7
                or "megaways" in name_lower
                or "trueways" in name_lower
            )

            if not isinstance(default_bet, (int, float)):
                raise ValueError("BGaming init no entregó una apuesta utilizable.")

            legacy_line_bets = is_line_bet_init(init_data)
            legacy_line_count = line_bet_count(init_data)
            if legacy_line_bets:
                bet_source = f"line_bets:{legacy_line_count} líneas"
                variable_layout = False

            previous_total = balance_total(init_data)
            register_mode(
                {
                    "id": "SPIN",
                    "kind": "SPIN",
                    "observed": True,
                    "wire_command": "spin",
                }
            )

            purchase_modes = (
                []
                if legacy_line_bets
                else discover_purchase_modes(init_data)
            )
            for purchase in purchase_modes:
                name = str(purchase["name"])
                mode_id = f"PURCHASE_{name.upper()}"
                mode_spec = {
                    "id": mode_id,
                    "kind": "PURCHASE",
                    "purchase": purchase,
                }
                mode_specs.append(mode_spec)
                register_mode(
                    {
                        "id": mode_id,
                        "kind": "PURCHASE",
                        "observed": True,
                        "wire_command": "spin",
                        "purchased_feature": name,
                        "feature_multiplier": purchase["feature_multiplier"],
                        "base_multiplier": purchase["base_multiplier"],
                        "cost_multiplier": purchase["cost_multiplier"],
                        "source": "options.feature_options.feature_multipliers",
                    }
                )

            pending_actions.update(pending_flow_actions(init_data))
            progress(
                f"[{game.name}] INIT OK: identifier={runtime.identifier}, "
                f"bet={default_bet} ({bet_source or 'unknown'}), "
                f"layout={expected_reels or '?'}x{expected_rows or '?'}, "
                f"balance_total={previous_total if previous_total is not None else '—'}, "
                f"perfil={'line-bets' if legacy_line_bets else 'api-v2'}, "
                f"compras={len(purchase_modes)}"
                + (
                    f", warnings={len(init_warnings)}"
                    if init_warnings
                    else ""
                )
            )
            for purchase in purchase_modes:
                progress(
                    f"[{game.name}] COMPRA detectada: {purchase['name']} "
                    f"x{purchase['cost_multiplier']:g} de la apuesta base "
                    "(HAR feature_multipliers)."
                )
        except Exception as exc:
            message = sanitize_error_text(f"{type(exc).__name__}: {exc}")
            errors.append(message)
            progress(f"[{game.name}] BGaming bootstrap/init ERROR: {message}")

        def send_api_command(
            command: str,
            *,
            options_payload: dict[str, Any] | None = None,
            extra_data_payload: dict[str, Any] | None = None,
        ):
            nonlocal rows_required
            merged_options = (
                dict(options_payload)
                if isinstance(options_payload, dict)
                else None
            )
            if (
                rows_required
                and not legacy_line_bets
                and isinstance(expected_rows, int)
                and expected_rows > 0
            ):
                if merged_options is None:
                    merged_options = {}
                merged_options.setdefault("rows", expected_rows)

            try:
                return post_command(
                    runtime,
                    command,
                    timeout_s=timeout_s,
                    options=merged_options,
                    extra_data=extra_data_payload,
                )
            except requests.HTTPError as exc:
                status = (
                    int(exc.response.status_code)
                    if exc.response is not None
                    else 0
                )
                can_retry_rows = (
                    status == 422
                    and not legacy_line_bets
                    and isinstance(expected_rows, int)
                    and expected_rows > 0
                    and (
                        merged_options is None
                        or "rows" not in merged_options
                    )
                )
                if not can_retry_rows:
                    raise

                retry_options = (
                    dict(merged_options)
                    if isinstance(merged_options, dict)
                    else {}
                )
                retry_options["rows"] = expected_rows
                progress(
                    f"[{game.name}] HTTP 422: reintentando {command} "
                    f"con rows={expected_rows} según layout del init."
                )
                result = post_command(
                    runtime,
                    command,
                    timeout_s=timeout_s,
                    options=retry_options,
                    extra_data=extra_data_payload,
                )
                rows_required = True
                progress(
                    f"[{game.name}] Perfil API aprendido: "
                    f"rows={expected_rows} requerido en comandos de juego."
                )
                return result

        requested_total = repetitions * len(mode_specs)

        if runtime is not None and isinstance(default_bet, (int, float)):
            for mode_spec in mode_specs:
                mode_id = str(mode_spec["id"])
                mode_kind = str(mode_spec["kind"])
                purchase = mode_spec.get("purchase")
                purchase_name = (
                    str(purchase.get("name") or "")
                    if isinstance(purchase, dict)
                    else ""
                )

                for repetition in range(1, repetitions + 1):
                    if stop_event.is_set():
                        break

                    attempt_dir = (
                        run_dir
                        / mode_id
                        / f"attempt-{repetition:03d}"
                    )
                    attempt_started = time.monotonic()
                    warnings: list[str] = []
                    wire_steps = 0
                    terminal = False
                    last_status_code: int | None = None
                    final_flow_state = ""
                    final_proof: dict[str, Any] = {}
                    first_response_received = False

                    try:
                        if legacy_line_bets:
                            spin_options = {
                                "bets": build_line_bets(init_data, default_bet)
                            }
                            request_extra_data = {
                                "client_seed": secrets.randbelow(100000),
                                "round_series_id": runtime.round_series_id,
                            }
                            expected_debit = (
                                float(default_bet) * float(legacy_line_count)
                            )
                        else:
                            spin_options = {"bet": default_bet}
                            if purchase_name:
                                spin_options["purchased_feature"] = purchase_name
                            request_extra_data = None
                            expected_debit = purchase_expected_debit(
                                default_bet,
                                purchase if isinstance(purchase, dict) else None,
                            )

                        response, request_payload, data = send_api_command(
                            "spin",
                            options_payload=spin_options,
                            extra_data_payload=request_extra_data,
                        )
                        first_response_received = True
                        responded_attempts += 1
                        wire_steps += 1
                        last_status_code = int(response.status_code)

                        self._write_json(attempt_dir / "request.json", request_payload)
                        self._write_json(attempt_dir / "response.json", data)
                        self._write_json(
                            attempt_dir / f"step-{wire_steps:03d}-request.json",
                            request_payload,
                        )
                        self._write_json(
                            attempt_dir / f"step-{wire_steps:03d}-response.json",
                            data,
                        )

                        if legacy_line_bets:
                            line_warnings, inferred_win = validate_line_spin(
                                data,
                                requested_line_bet=default_bet,
                                line_count=legacy_line_count,
                                previous_balance_total=previous_total,
                            )
                            warnings.extend(line_warnings)
                            current_total = balance_total(data)
                            if current_total is not None:
                                previous_total = current_total
                            game_state = data.get("game")
                            if not isinstance(game_state, dict):
                                game_state = {}
                            commands = data.get("available_commands")
                            command_names = (
                                {str(item) for item in commands}
                                if isinstance(commands, list)
                                else set()
                            )
                            terminal = (
                                str(game_state.get("state") or "") == "closed"
                                and str(game_state.get("action") or "") == "spin"
                                and "spin" in command_names
                            )
                            proof = {
                                "runtime": "legacy-line-bets",
                                "mode_id": mode_id,
                                "step": wire_steps,
                                "line_bet": default_bet,
                                "line_count": legacy_line_count,
                                "total_debit": expected_debit,
                                "inferred_win": inferred_win,
                                "balance_total": current_total,
                                "game_state": game_state.get("state"),
                                "game_action": game_state.get("action"),
                                "response_sha256": spin_remote_proof(data).get(
                                    "response_sha256"
                                ),
                            }
                            self._write_json(
                                attempt_dir / "remote-proof.json",
                                proof,
                            )
                            validated = terminal and not warnings
                            successes += int(validated)
                            global_warnings.extend(warnings)
                            elapsed_ms = (
                                time.monotonic() - attempt_started
                            ) * 1000.0
                            attempts.append(
                                SpinAttempt(
                                    number=repetition,
                                    ok=True,
                                    mode_id=mode_id,
                                    mode_kind=mode_kind,
                                    status_code=last_status_code,
                                    elapsed_ms=elapsed_ms,
                                    symbol=runtime.identifier,
                                    endpoint=sanitize_session_url(runtime.api_url),
                                    na=str(game_state.get("state") or ""),
                                    terminal=terminal,
                                    wire_steps=wire_steps,
                                    warning="; ".join(warnings),
                                    artifact_dir=str(attempt_dir),
                                )
                            )
                            progress(
                                f"[{game.name}] {mode_id} {repetition}/{repetitions}: "
                                f"{'OK' if validated else 'PARCIAL'} "
                                f"{elapsed_ms:.0f} ms, perfil=line-bets, "
                                f"líneas={legacy_line_count}, line_bet={default_bet}, "
                                f"debit={expected_debit:g}, "
                                f"win_inferido={inferred_win if inferred_win is not None else '—'}, "
                                f"balance={current_total if current_total is not None else '—'}"
                                + (
                                    f", diagnóstico={' | '.join(warnings[:3])}"
                                    if warnings
                                    else ""
                                )
                            )
                            continue

                        warnings.extend(
                            validate_spin(
                                data,
                                requested_bet=default_bet,
                                previous_balance_total=previous_total,
                                expected_reels=expected_reels,
                                expected_rows=expected_rows,
                                command="spin",
                                expected_debit=expected_debit,
                                variable_layout=variable_layout,
                            )
                        )

                        flow = data.get("flow")
                        if not isinstance(flow, dict):
                            flow = {}
                        purchased = flow.get("purchased_feature")
                        actual_purchase = (
                            str(purchased.get("name") or "")
                            if isinstance(purchased, dict)
                            else ""
                        )
                        if (
                            purchase_name
                            and not purchase_names_equivalent(
                                purchase_name,
                                actual_purchase,
                            )
                        ):
                            warnings.append(
                                f"purchased_feature devuelta={actual_purchase!r}, "
                                f"solicitada={purchase_name!r}"
                            )
                        if not purchase_name and actual_purchase:
                            warnings.append(
                                f"spin base devolvió purchased_feature inesperada: "
                                f"{actual_purchase!r}"
                            )

                        for action_name in pending_flow_actions(data):
                            pending_actions.add(action_name)
                            register_mode(
                                {
                                    "id": action_name.upper(),
                                    "kind": "FEATURE",
                                    "observed": False,
                                    "wire_command": action_name,
                                }
                            )

                        proof = spin_remote_proof(data)
                        proof["mode_id"] = mode_id
                        proof["step"] = wire_steps
                        proof["expected_debit"] = expected_debit
                        self._write_json(
                            attempt_dir / f"step-{wire_steps:03d}-proof.json",
                            proof,
                        )
                        final_proof = proof

                        remote_identity = (
                            proof.get("round_id"),
                            proof.get("last_action_id"),
                            str(proof.get("response_sha256") or ""),
                        )
                        if (
                            previous_remote_identity is not None
                            and remote_identity == previous_remote_identity
                        ):
                            warnings.append(
                                "respuesta remota idéntica a la anterior "
                                "(round_id/action_id/hash sin cambios)"
                            )
                        previous_remote_identity = remote_identity

                        current_total = balance_total(data)
                        if current_total is not None:
                            previous_total = current_total

                        trigger_round_id = flow.get("round_id")
                        continuation_guard = 256
                        while not stop_event.is_set():
                            state = str(flow.get("state") or "")
                            actions = flow.get("available_actions")
                            action_names = (
                                {str(action) for action in actions}
                                if isinstance(actions, list)
                                else set()
                            )
                            continuation_command = flow_continuation_command(
                                {"flow": flow}
                            )
                            if not continuation_command:
                                break
                            if continuation_command not in action_names:
                                warnings.append(
                                    f"estado {state} sin available_actions="
                                    f"{continuation_command}"
                                )
                                break
                            if wire_steps >= continuation_guard:
                                warnings.append(
                                    f"guard de continuaciones alcanzado ({continuation_guard})"
                                )
                                break

                            register_mode(
                                {
                                    "id": continuation_command.upper(),
                                    "kind": "CONTINUATION",
                                    "observed": True,
                                    "wire_command": continuation_command,
                                }
                            )

                            before_total = previous_total
                            cont_response, cont_request, cont_data = send_api_command(
                                continuation_command,
                            )
                            wire_steps += 1
                            last_status_code = int(cont_response.status_code)
                            self._write_json(
                                attempt_dir / f"step-{wire_steps:03d}-request.json",
                                cont_request,
                            )
                            self._write_json(
                                attempt_dir / f"step-{wire_steps:03d}-response.json",
                                cont_data,
                            )

                            cont_warnings = validate_spin(
                                cont_data,
                                requested_bet=default_bet,
                                previous_balance_total=before_total,
                                expected_reels=expected_reels,
                                expected_rows=expected_rows,
                                command=continuation_command,
                                expected_debit=0,
                                variable_layout=variable_layout,
                            )
                            warnings.extend(cont_warnings)

                            cont_flow = cont_data.get("flow")
                            if not isinstance(cont_flow, dict):
                                cont_flow = {}
                            if (
                                trigger_round_id is not None
                                and cont_flow.get("round_id") != trigger_round_id
                            ):
                                warnings.append(
                                    "round_id cambió dentro de la continuación: "
                                    f"{trigger_round_id!r}→{cont_flow.get('round_id')!r}"
                                )

                            for action_name in pending_flow_actions(cont_data):
                                pending_actions.add(action_name)
                                register_mode(
                                    {
                                        "id": action_name.upper(),
                                        "kind": "FEATURE",
                                        "observed": False,
                                        "wire_command": action_name,
                                    }
                                )

                            cont_proof = spin_remote_proof(cont_data)
                            cont_proof["mode_id"] = mode_id
                            cont_proof["step"] = wire_steps
                            cont_proof["expected_debit"] = 0
                            if continuation_command == "preselection_game":
                                cont_proof["bonus_multiplier"] = (
                                    preselection_multiplier(cont_data)
                                )
                            self._write_json(
                                attempt_dir / f"step-{wire_steps:03d}-proof.json",
                                cont_proof,
                            )
                            final_proof = cont_proof

                            remote_identity = (
                                cont_proof.get("round_id"),
                                cont_proof.get("last_action_id"),
                                str(cont_proof.get("response_sha256") or ""),
                            )
                            if (
                                previous_remote_identity is not None
                                and remote_identity == previous_remote_identity
                            ):
                                warnings.append(
                                    f"respuesta remota {continuation_command} "
                                    "idéntica a la anterior"
                                )
                            previous_remote_identity = remote_identity

                            current_total = balance_total(cont_data)
                            if current_total is not None:
                                previous_total = current_total

                            if continuation_command == "freespin":
                                features = cont_data.get("features")
                                freespins_left = (
                                    features.get("freespins_left")
                                    if isinstance(features, dict)
                                    else None
                                )
                                progress(
                                    f"[{game.name}] {mode_id} FREESPIN "
                                    f"step={wire_steps}, "
                                    f"round={cont_proof.get('round_id') or '—'}, "
                                    f"action={cont_proof.get('last_action_id') or '—'}, "
                                    f"left={freespins_left if freespins_left is not None else '—'}, "
                                    f"win={cont_proof.get('win') if cont_proof.get('win') is not None else '—'}, "
                                    f"balance={current_total if current_total is not None else '—'}"
                                )
                            elif continuation_command == "preselection_game":
                                multiplier = preselection_multiplier(cont_data)
                                progress(
                                    f"[{game.name}] {mode_id} PRESELECTION "
                                    f"step={wire_steps}, "
                                    f"round={cont_proof.get('round_id') or '—'}, "
                                    f"action={cont_proof.get('last_action_id') or '—'}, "
                                    f"multiplier={multiplier if multiplier is not None else '—'}, "
                                    f"win={cont_proof.get('win') if cont_proof.get('win') is not None else '—'}, "
                                    f"balance={current_total if current_total is not None else '—'}"
                                )
                            else:
                                progress(
                                    f"[{game.name}] {mode_id} "
                                    f"{continuation_command.upper()} "
                                    f"step={wire_steps}, "
                                    f"round={cont_proof.get('round_id') or '—'}, "
                                    f"action={cont_proof.get('last_action_id') or '—'}, "
                                    f"state={cont_proof.get('flow_state') or '—'}, "
                                    f"win={cont_proof.get('win') if cont_proof.get('win') is not None else '—'}, "
                                    f"balance={current_total if current_total is not None else '—'}"
                                )

                            data = cont_data
                            flow = cont_flow

                        final_flow_state = str(flow.get("state") or "")
                        final_actions = flow.get("available_actions")
                        final_action_names = (
                            {str(action) for action in final_actions}
                            if isinstance(final_actions, list)
                            else set()
                        )
                        terminal = (
                            final_flow_state == "closed"
                            and "spin" in final_action_names
                        )
                        if not terminal and not stop_event.is_set():
                            warnings.append(
                                f"estado final BGaming no terminal: "
                                f"state={final_flow_state!r}, actions={sorted(final_action_names)!r}"
                            )

                        validated = terminal and not warnings
                        successes += int(validated)
                        global_warnings.extend(warnings)

                        self._write_json(
                            attempt_dir / "remote-proof.json",
                            final_proof,
                        )
                        elapsed_ms = (time.monotonic() - attempt_started) * 1000.0
                        attempts.append(
                            SpinAttempt(
                                number=repetition,
                                ok=first_response_received,
                                mode_id=mode_id,
                                mode_kind=mode_kind,
                                status_code=last_status_code,
                                elapsed_ms=elapsed_ms,
                                symbol=runtime.identifier,
                                endpoint=sanitize_session_url(runtime.api_url),
                                na=final_flow_state,
                                terminal=terminal,
                                wire_steps=wire_steps,
                                warning="; ".join(warnings),
                                artifact_dir=str(attempt_dir),
                            )
                        )

                        progress(
                            f"[{game.name}] {mode_id} {repetition}/{repetitions}: "
                            f"{'OK' if validated else 'PARCIAL'} {elapsed_ms:.0f} ms, "
                            f"steps={wire_steps}, "
                            f"HTTP={last_status_code or '—'}, "
                            f"round={final_proof.get('round_id') or '—'}, "
                            f"action={final_proof.get('last_action_id') or '—'}, "
                            f"bet={default_bet}, "
                            f"debit={expected_debit:g}, "
                            f"win={final_proof.get('win') if final_proof.get('win') is not None else '—'}, "
                            f"balance={previous_total if previous_total is not None else '—'}, "
                            f"seed={final_proof.get('storage_seed') if final_proof.get('storage_seed') is not None else '—'}, "
                            f"resp={final_proof.get('response_sha256') or '—'}"
                            + (
                                f", warnings={len(warnings)}, "
                                f"diagnóstico={' | '.join(warnings[:3])}"
                                if warnings
                                else ""
                            )
                        )
                    except Exception as exc:
                        elapsed_ms = (time.monotonic() - attempt_started) * 1000.0
                        message = sanitize_error_text(f"{type(exc).__name__}: {exc}")
                        errors.append(message)
                        attempts.append(
                            SpinAttempt(
                                number=repetition,
                                ok=False,
                                mode_id=mode_id,
                                mode_kind=mode_kind,
                                elapsed_ms=elapsed_ms,
                                symbol=game.symbol,
                                endpoint=(
                                    sanitize_session_url(runtime.api_url)
                                    if runtime is not None
                                    else ""
                                ),
                                terminal=False,
                                wire_steps=wire_steps,
                                error=message,
                                artifact_dir=str(attempt_dir),
                            )
                        )
                        progress(
                            f"[{game.name}] {mode_id} {repetition}/{repetitions}: "
                            f"ERROR {message}"
                        )

                if stop_event.is_set():
                    break
        else:
            for repetition in range(1, repetitions + 1):
                message = errors[0] if errors else "BGaming bootstrap/init no disponible."
                attempts.append(
                    SpinAttempt(
                        number=repetition,
                        ok=False,
                        mode_id="SPIN",
                        mode_kind="SPIN",
                        symbol=game.symbol,
                        terminal=False,
                        error=message,
                        artifact_dir=str(run_dir / "SPIN" / f"attempt-{repetition:03d}"),
                    )
                )

        elapsed_total = (time.monotonic() - started) * 1000.0
        attempted = len(attempts)

        if (
            attempted
            and successes == requested_total
            and not pending_actions
            and not errors
        ):
            # Attempt validation already incorporates fatal protocol warnings.
            # Init-level diagnostics must not downgrade a run that completed
            # every requested mode successfully.
            status = "OK"
            error = ""
        elif responded_attempts:
            status = "PARCIAL"
            detail: list[str] = [
                f"BGaming respondió {responded_attempts}/{requested_total} intentos; "
                f"modos terminales validados={successes}/{requested_total}."
            ]
            if purchase_modes:
                detail.append(
                    "Compras probadas: "
                    + ", ".join(
                        f"{mode['name']} x{mode['cost_multiplier']:g}"
                        for mode in purchase_modes
                    )
                    + "."
                )
            if pending_actions:
                detail.append(
                    "Acciones aún no clasificadas: "
                    + ", ".join(sorted(pending_actions))
                    + "."
                )
            if global_warnings:
                unique = list(dict.fromkeys(global_warnings))
                detail.append("Diagnóstico: " + " | ".join(unique[:6]))
            error = " ".join(detail)
        else:
            status = "ERROR"
            error = errors[0] if errors else "No se completó ninguna tirada BGaming."

        return GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=requested_total,
            successful_spins=successes,
            failed_spins=max(0, requested_total - successes),
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

