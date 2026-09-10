from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import requests

from tester_spin.models import Game, GameTestResult, SpinAttempt, utc_now_iso
from tester_spin.providers.base import Progress
from tester_spin.providers.bgaming.hyperhive import run_hyperhive_test
from tester_spin.providers.bgaming.profile import (
    API_V2,
    HYPERHIVE,
    LEGACY_LINES,
    SWITCHABLE,
    UNKNOWN,
    BGamingProfile,
    classify_runtime,
    discover_profile,
    load_profile,
    profile_fingerprint,
    save_profile,
)
from tester_spin.providers.bgaming.runtime import (
    balance_total,
    bootstrap_game,
    build_line_bets,
    discover_purchase_modes,
    flow_continuation_command,
    http_error_evidence,
    is_line_bet_init,
    line_bet_count,
    pending_flow_actions,
    post_command,
    preselection_multiplier,
    provider_error_envelope,
    purchase_expected_debit,
    purchase_names_equivalent,
    resolve_base_bet,
    infer_missing_wire_options,
    infer_observed_debit,
    is_demo_url,
    legacy_safe_terminal_command,
    resolve_fresh_demo_url,
    runtime_shape_summary,
    sanitize_error_text,
    sanitize_options,
    sanitize_session_url,
    spin_remote_proof,
    validate_init,
    validate_line_spin,
    validate_spin,
)
from tester_spin.providers.bgaming.switchable import run_switchable_container_test


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _fmt_number(value: Any) -> str:
    return f"{float(value):g}" if isinstance(value, (int, float)) else "unknown"


class BGamingExecutionMixin:
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

        game_json = self.game_dir(game) / "game.json"
        public_url = ""
        if game_json.is_file():
            try:
                persisted_game = json.loads(game_json.read_text(encoding="utf-8"))
                if isinstance(persisted_game, dict):
                    public_url = str(persisted_game.get("public_url") or "").strip()
            except Exception:
                public_url = ""

        session = self._new_session()
        execution_url = game.url
        if not is_demo_url(execution_url):
            try:
                fresh_demo_url = resolve_fresh_demo_url(
                    session,
                    execution_url,
                    timeout_s=timeout_s,
                )
            except Exception as exc:
                fresh_demo_url = ""
                progress(
                    f"[{game.name}] resolución demo BGaming: "
                    f"{sanitize_error_text(f'{type(exc).__name__}: {exc}')}"
                )
            if fresh_demo_url:
                execution_url = fresh_demo_url
                progress(
                    f"[{game.name}] demo efímero resuelto en memoria; "
                    "credenciales de sesión no se persistirán."
                )
            else:
                session.close()
                elapsed = (time.monotonic() - started) * 1000.0
                return GameTestResult(
                    provider=self.key,
                    slug=game.slug,
                    game_name=game.name,
                    game_url=game.url,
                    requested_spins=repetitions,
                    successful_spins=0,
                    failed_spins=repetitions,
                    status="SIN_DEMO",
                    symbol=game.symbol,
                    started_at=started_iso,
                    finished_at=utc_now_iso(),
                    elapsed_ms=elapsed,
                    error="BGaming: juego catalogado sin Play Demo resoluble.",
                    run_dir=str(run_dir),
                )

        persisted_profile = load_profile(game_json)
        active_profile: BGamingProfile | None = None

        def persist_profile_snapshot() -> None:
            if active_profile is None:
                return
            save_profile(game_json, active_profile)
            _write_json(run_dir / "profile.json", active_profile.to_dict())

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
        api_profile_checked = False
        learned_wire_options: dict[str, Any] = {}
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
            try:
                runtime = bootstrap_game(
                    session,
                    execution_url,
                    timeout_s=timeout_s,
                )
            except requests.HTTPError as launch_exc:
                status_code = (
                    int(launch_exc.response.status_code)
                    if launch_exc.response is not None
                    else 0
                )
                if (
                    status_code not in {404, 410}
                    or not public_url
                    or public_url == execution_url
                ):
                    raise
                progress(
                    f"[{game.name}] launch guardado devolvió HTTP {status_code}; "
                    "resolviendo un demo fresco desde la ficha pública."
                )
                fresh_demo_url = resolve_fresh_demo_url(
                    session,
                    public_url,
                    timeout_s=timeout_s,
                )
                if not fresh_demo_url or fresh_demo_url == execution_url:
                    session.close()
                    elapsed = (time.monotonic() - started) * 1000.0
                    return GameTestResult(
                        provider=self.key,
                        slug=game.slug,
                        game_name=game.name,
                        game_url=game.url,
                        requested_spins=repetitions,
                        successful_spins=0,
                        failed_spins=repetitions,
                        status="SIN_DEMO",
                        symbol=game.symbol,
                        started_at=started_iso,
                        finished_at=utc_now_iso(),
                        elapsed_ms=elapsed,
                        error=(
                            "BGaming: launch demo guardado expiró y la ficha "
                            "pública no expuso otro demo validable."
                        ),
                        run_dir=str(run_dir),
                    )
                execution_url = fresh_demo_url
                runtime = bootstrap_game(
                    session,
                    execution_url,
                    timeout_s=timeout_s,
                )
            game.symbol = runtime.identifier

            _write_json(
                run_dir / "bootstrap.json",
                {
                    "identifier": runtime.identifier,
                    "launch_url": sanitize_session_url(runtime.launch_url),
                    "api_url": sanitize_session_url(runtime.api_url),
                    "options": sanitize_options(runtime.options),
                    "round_series_id": runtime.round_series_id,
                },
            )

            bootstrap_classification = classify_runtime(runtime)
            if bootstrap_classification.family == HYPERHIVE:
                active_profile = discover_profile(
                    runtime,
                    {},
                    timeout_s=timeout_s,
                    persisted=persisted_profile,
                )
                persist_profile_snapshot()
                progress(
                    f"[{game.name}] runtime={active_profile.family} "
                    f"confidence={active_profile.confidence:.2f}; "
                    "cambiando a executor JSON-RPC."
                )
                try:
                    result = run_hyperhive_test(
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
                    if result.status == "OK":
                        active_profile.validated = True
                        persist_profile_snapshot()
                    return result
                finally:
                    session.close()

            _init_response, init_request, init_data = post_command(
                runtime,
                "init",
                timeout_s=timeout_s,
            )
            _write_json(run_dir / "init-request.json", init_request)
            _write_json(run_dir / "init-response.json", init_data)

            init_errors = provider_error_envelope(init_data)
            if init_errors is not None:
                _write_json(run_dir / "init-provider-error.json", init_errors)
                progress(
                    f"[{game.name}] init devolvió errors; renovando launch/sesión "
                    "una vez antes de clasificar runtime."
                )
                recovered = False
                if public_url:
                    recovery_session = self._new_session()
                    try:
                        fresh_demo_url = resolve_fresh_demo_url(
                            recovery_session,
                            public_url,
                            timeout_s=timeout_s,
                        )
                        if fresh_demo_url:
                            recovered_runtime = bootstrap_game(
                                recovery_session,
                                fresh_demo_url,
                                timeout_s=timeout_s,
                            )
                            (
                                _recovery_response,
                                recovery_request,
                                recovery_data,
                            ) = post_command(
                                recovered_runtime,
                                "init",
                                timeout_s=timeout_s,
                            )
                            _write_json(
                                run_dir / "init-recovery-request.json",
                                recovery_request,
                            )
                            _write_json(
                                run_dir / "init-recovery-response.json",
                                recovery_data,
                            )
                            if provider_error_envelope(recovery_data) is None:
                                old_session = session
                                session = recovery_session
                                runtime = recovered_runtime
                                execution_url = fresh_demo_url
                                init_data = recovery_data
                                old_session.close()
                                recovered = True
                    except Exception as recovery_exc:
                        _write_json(
                            run_dir / "init-recovery-error.json",
                            {
                                "error": sanitize_error_text(
                                    f"{type(recovery_exc).__name__}: {recovery_exc}"
                                )
                            },
                        )
                    finally:
                        if not recovered:
                            recovery_session.close()

                if not recovered:
                    raise ValueError(
                        "BGaming init devolvió un envelope errors y la renovación "
                        f"de sesión no lo resolvió: {str(init_errors)[:600]}"
                    )
                progress(
                    f"[{game.name}] init recuperado con una sesión demo fresca."
                )

            active_profile = discover_profile(
                runtime,
                init_data,
                timeout_s=timeout_s,
                persisted=persisted_profile,
            )
            persist_profile_snapshot()
            progress(
                f"[{game.name}] perfil BGaming: family={active_profile.family}, "
                f"confidence={active_profile.confidence:.2f}, "
                f"source={active_profile.source or '—'}, "
                f"fingerprint={profile_fingerprint(active_profile)}."
            )

            if active_profile.family == SWITCHABLE:
                progress(
                    f"[{game.name}] contenedor switchable confirmado por múltiples "
                    "señales; cambiando a executor de variantes."
                )
                try:
                    result = run_switchable_container_test(
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
                    if result.status == "OK":
                        active_profile.validated = True
                        persist_profile_snapshot()
                    return result
                finally:
                    session.close()

            if active_profile.family == UNKNOWN:
                shape = runtime_shape_summary(init_data)
                _write_json(run_dir / "unknown-runtime-shape.json", shape)
                progress(
                    f"[{game.name}] runtime BGaming desconocido: "
                    f"keys={shape.get('top_level_keys', [])}; "
                    "evidencia estructural guardada sin ejecutar comandos."
                )
                raise ValueError(
                    "BGaming: runtime no clasificado con evidencia suficiente; "
                    "RAW y mapa estructural preservados sin ejecutar comandos de juego."
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

            variable_layout = bool(
                active_profile.variable_layout if active_profile is not None else False
            )

            if not isinstance(default_bet, (int, float)):
                raise ValueError("BGaming init no entregó una apuesta utilizable.")

            legacy_line_bets = (
                active_profile is not None and active_profile.family == LEGACY_LINES
            )
            legacy_line_count = (
                active_profile.line_count
                if active_profile is not None and active_profile.line_count
                else line_bet_count(init_data)
            )
            if active_profile is not None and active_profile.family == API_V2:
                learned_wire_options.update(active_profile.spin_options)
                rows_required = active_profile.rows_required
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
            client_purchase_features = (
                set(active_profile.purchase_features)
                if active_profile is not None
                else set()
            )
            client_purchase_contract_available = bool(client_purchase_features)

            for purchase in purchase_modes:
                name = str(purchase["name"])
                mode_id = f"PURCHASE_{name.upper()}"
                client_observed = any(
                    purchase_names_equivalent(name, observed)
                    or purchase_names_equivalent(observed, name)
                    for observed in client_purchase_features
                )
                executable = client_observed
                if executable:
                    mode_specs.append(
                        {
                            "id": mode_id,
                            "kind": "PURCHASE",
                            "purchase": purchase,
                        }
                    )
                else:
                    pending_actions.add(mode_id)

                register_mode(
                    {
                        "id": mode_id,
                        "kind": "PURCHASE",
                        "observed": True,
                        "client_observed": client_observed,
                        "executable": executable,
                        "discovery_state": (
                            "CLIENT_OBSERVED"
                            if client_observed
                            else "ADVERTISED_ONLY"
                        ),
                        "wire_command": "spin",
                        "purchased_feature": name,
                        "feature_multiplier": purchase["feature_multiplier"],
                        "base_multiplier": purchase["base_multiplier"],
                        "cost_multiplier": purchase["cost_multiplier"],
                        "source": (
                            "init.feature_multipliers+client"
                            if client_observed
                            else "options.feature_options.feature_multipliers"
                        ),
                    }
                )

            pending_actions.update(pending_flow_actions(init_data))
            progress(
                f"[{game.name}] INIT OK: identifier={runtime.identifier}, "
                f"bet={default_bet} ({bet_source or 'unknown'}), "
                f"layout={expected_reels or '?'}x{expected_rows or '?'}, "
                f"balance_total={previous_total if previous_total is not None else '—'}, "
                f"perfil={active_profile.family if active_profile is not None else 'unknown'}, "
                f"compras={len(purchase_modes)}"
                + (
                    f", warnings={len(init_warnings)}"
                    if init_warnings
                    else ""
                )
            )
            for purchase in purchase_modes:
                purchase_name = str(purchase["name"])
                mode_id = f"PURCHASE_{purchase_name.upper()}"
                executable = mode_id not in pending_actions
                progress(
                    f"[{game.name}] COMPRA detectada: {purchase_name} "
                    f"x{_fmt_number(purchase.get('cost_multiplier'))} de la apuesta base "
                    f"(source={purchase.get('base_source') or 'unknown'}, "
                    f"wire={'ejecutable' if executable else 'sin contrato cliente'})."
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
            nonlocal api_profile_checked
            merged_options = (
                dict(options_payload)
                if isinstance(options_payload, dict)
                else None
            )

            # Global options are learned from the client bundle (for example a
            # selectable line mode). Command-specific options are learned only
            # from explicit client/server evidence and never from a title/slug.
            if learned_wire_options and not legacy_line_bets and command == "spin":
                if merged_options is None:
                    merged_options = {}
                for key, value in learned_wire_options.items():
                    merged_options.setdefault(key, value)

            if active_profile is not None and not legacy_line_bets:
                specific = active_profile.command_options.get(command)
                if isinstance(specific, dict) and specific:
                    if merged_options is None:
                        merged_options = {}
                    for key, value in specific.items():
                        merged_options.setdefault(key, value)

                # Backward compatibility for profiles learned by the older
                # global rows_required mechanism. New evidence is per-command.
                if (
                    active_profile.rows_required
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
                evidence = http_error_evidence(exc.response)
                try:
                    _write_json(
                        attempt_dir / f"http-{command}-{status or 'error'}.json",
                        evidence,
                    )
                except (NameError, UnboundLocalError):
                    pass
                if active_profile is not None:
                    active_profile.discovery_diagnostics.append(
                        {
                            "kind": "http-command-error",
                            "command": command,
                            **evidence,
                        }
                    )
                    persist_profile_snapshot()

                if status != 422 or legacy_line_bets:
                    raise

                if active_profile is not None:
                    active_profile.validated = False

                missing: dict[str, Any] = {}
                refreshed: BGamingProfile | None = None

                if not api_profile_checked:
                    api_profile_checked = True
                    refreshed = discover_profile(
                        runtime,
                        init_data,
                        timeout_s=timeout_s,
                        persisted=None,
                    )
                    for key, value in refreshed.spin_options.items():
                        if merged_options is None or key not in merged_options:
                            missing.setdefault(key, value)

                validation_missing: dict[str, Any] = {}
                validation_evidence = ""
                if exc.response is not None:
                    validation_missing, validation_evidence = infer_missing_wire_options(
                        exc.response,
                        init_data,
                    )
                    for key, value in validation_missing.items():
                        if merged_options is None or key not in merged_options:
                            missing.setdefault(key, value)

                if active_profile is not None:
                    if refreshed is not None:
                        active_profile.source = refreshed.source
                        active_profile.bundle_sha256 = refreshed.bundle_sha256
                        active_profile.discovery_diagnostics = list(
                            refreshed.discovery_diagnostics
                        )
                    if validation_missing:
                        command_profile = active_profile.command_options.setdefault(
                            command,
                            {},
                        )
                        command_profile.update(validation_missing)
                        active_profile.discovery_diagnostics.append(
                            {
                                "kind": "http-validation",
                                "status": 422,
                                "command": command,
                                "inferred_options": dict(validation_missing),
                                "evidence": validation_evidence,
                            }
                        )
                    persist_profile_snapshot()

                if not missing:
                    progress(
                        f"[{game.name}] HTTP 422 en {command}: el servidor no "
                        "identificó ninguna opción faltante utilizable; no se "
                        "adivinan campos desde layout."
                    )
                    raise

                retry_options = (
                    dict(merged_options)
                    if isinstance(merged_options, dict)
                    else {}
                )
                retry_options.update(missing)
                progress(
                    f"[{game.name}] HTTP 422: contrato faltante inferido por evidencia "
                    f"→ {missing!r}; reintentando {command} una vez."
                )
                try:
                    result = post_command(
                        runtime,
                        command,
                        timeout_s=timeout_s,
                        options=retry_options,
                        extra_data=extra_data_payload,
                    )
                except requests.HTTPError:
                    # Keep explicitly inferred command options in the persisted
                    # profile even if the demo gateway fails afterwards. The
                    # next fresh session can start with the corrected wire shape.
                    raise
                else:
                    if refreshed is not None:
                        learned_wire_options.update(
                            {
                                key: value
                                for key, value in refreshed.spin_options.items()
                                if key in missing
                            }
                        )
                        if active_profile is not None:
                            active_profile.spin_options.update(
                                {
                                    key: value
                                    for key, value in refreshed.spin_options.items()
                                    if key in missing
                                }
                            )
                    if active_profile is not None:
                        persist_profile_snapshot()
                    progress(
                        f"[{game.name}] Perfil API actualizado para {command}: "
                        f"{missing!r}."
                    )
                    return result

        def refresh_api_session(reason: str) -> None:
            nonlocal session, runtime, init_data, default_bet
            nonlocal previous_total, expected_reels, expected_rows

            if runtime is None or active_profile is None or active_profile.family != API_V2:
                return

            new_session = self._new_session()
            try:
                new_runtime = bootstrap_game(
                    new_session,
                    execution_url,
                    timeout_s=timeout_s,
                )
                _response, init_request, new_init = post_command(
                    new_runtime,
                    "init",
                    timeout_s=timeout_s,
                )
                new_classification = classify_runtime(new_runtime, new_init)
                if new_classification.family != API_V2:
                    raise ValueError(
                        "BGaming: runtime cambió de familia durante aislamiento "
                        f"de modo: {new_classification.family!r}"
                    )
                if runtime.identifier and new_runtime.identifier != runtime.identifier:
                    raise ValueError(
                        "BGaming: identifier cambió durante aislamiento de modo: "
                        f"{runtime.identifier!r}→{new_runtime.identifier!r}"
                    )
                new_bet, _source = resolve_base_bet(new_init)
                if not isinstance(new_bet, (int, float)):
                    raise ValueError(
                        "BGaming: init fresco sin apuesta utilizable durante "
                        "aislamiento de modo."
                    )

                new_options = new_init.get("options")
                new_reels: int | None = None
                new_rows: int | None = None
                if isinstance(new_options, dict):
                    layout = new_options.get("layout")
                    if isinstance(layout, dict):
                        try:
                            new_reels = int(layout.get("reels"))
                        except (TypeError, ValueError):
                            new_reels = None
                        try:
                            new_rows = int(layout.get("rows"))
                        except (TypeError, ValueError):
                            new_rows = None

                old_session = session
                session = new_session
                runtime = new_runtime
                init_data = new_init
                default_bet = new_bet
                previous_total = balance_total(new_init)
                expected_reels = new_reels
                expected_rows = new_rows
                old_session.close()

                _write_json(run_dir / "last-refresh-init-request.json", init_request)
                _write_json(run_dir / "last-refresh-init-response.json", new_init)
                progress(
                    f"[{game.name}] sesión API-v2 fresca para {reason}; "
                    f"balance={previous_total if previous_total is not None else '—'}."
                )
            except Exception:
                new_session.close()
                raise

        requested_total = repetitions * len(mode_specs)

        if runtime is not None and isinstance(default_bet, (int, float)):
            runtime_needs_refresh = False
            for mode_index, mode_spec in enumerate(mode_specs):
                if mode_index > 0 and active_profile is not None and active_profile.family == API_V2:
                    runtime_needs_refresh = True
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
                        if runtime_needs_refresh:
                            refresh_api_session(f"modo {mode_id}")
                            runtime_needs_refresh = False

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

                        _write_json(attempt_dir / "request.json", request_payload)
                        _write_json(attempt_dir / "response.json", data)
                        _write_json(
                            attempt_dir / f"step-{wire_steps:03d}-request.json",
                            request_payload,
                        )
                        _write_json(
                            attempt_dir / f"step-{wire_steps:03d}-response.json",
                            data,
                        )

                        if legacy_line_bets:
                            line_warnings, inferred_win = validate_line_spin(
                                data,
                                requested_line_bet=default_bet,
                                line_count=legacy_line_count,
                                previous_balance_total=previous_total,
                                allow_safe_finish=True,
                            )
                            warnings.extend(line_warnings)
                            current_total = balance_total(data)
                            safe_terminal = legacy_safe_terminal_command(data)

                            if safe_terminal:
                                finish_response, finish_request, finish_data = send_api_command(
                                    safe_terminal,
                                    extra_data_payload={
                                        "round_series_id": runtime.round_series_id,
                                    },
                                )
                                wire_steps += 1
                                last_status_code = int(finish_response.status_code)
                                _write_json(
                                    attempt_dir / f"step-{wire_steps:03d}-request.json",
                                    finish_request,
                                )
                                _write_json(
                                    attempt_dir / f"step-{wire_steps:03d}-response.json",
                                    finish_data,
                                )
                                finish_state = finish_data.get("game")
                                if not isinstance(finish_state, dict):
                                    finish_state = {}
                                finish_commands = finish_data.get("available_commands")
                                finish_names = (
                                    {str(item) for item in finish_commands}
                                    if isinstance(finish_commands, list)
                                    else set()
                                )
                                terminal = (
                                    str(finish_state.get("state") or "") == "closed"
                                    and "spin" in finish_names
                                )
                                if not terminal:
                                    warnings.append(
                                        "legacy finish no dejó la ronda en estado cerrado "
                                        f"con spin disponible: state={finish_state.get('state')!r}, "
                                        f"commands={sorted(finish_names)!r}"
                                    )
                                data = finish_data
                                game_state = finish_state
                                command_names = finish_names
                                current_total = balance_total(finish_data)
                                progress(
                                    f"[{game.name}] {mode_id} FINISH step={wire_steps}, "
                                    f"state={game_state.get('state')!r}, "
                                    f"balance={current_total if current_total is not None else '—'}."
                                )
                            else:
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
                                    and "spin" in command_names
                                )

                            if current_total is not None:
                                previous_total = current_total
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
                            _write_json(
                                attempt_dir / "remote-proof.json",
                                proof,
                            )
                            validated = terminal and not warnings
                            if (
                                mode_id == "SPIN"
                                and active_profile is not None
                                and active_profile.validated != validated
                            ):
                                active_profile.validated = validated
                                persist_profile_snapshot()
                            successes += int(validated)
                            global_warnings.extend(warnings)
                            elapsed_ms = (
                                time.monotonic() - attempt_started
                            ) * 1000.0
                            attempts.append(
                                SpinAttempt(
                                    number=repetition,
                                    ok=validated,
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
                                f"debit={_fmt_number(expected_debit)}, "
                                f"win_inferido={inferred_win if inferred_win is not None else '—'}, "
                                f"balance={current_total if current_total is not None else '—'}"
                                + (
                                    f", diagnóstico={' | '.join(warnings[:3])}"
                                    if warnings
                                    else ""
                                )
                            )
                            continue

                        learn_purchase_debit = (
                            isinstance(purchase, dict)
                            and expected_debit is None
                        )
                        observed_purchase_debit = (
                            infer_observed_debit(data, previous_total)
                            if learn_purchase_debit
                            else None
                        )
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
                                allow_observed_debit=learn_purchase_debit,
                            )
                        )

                        if (
                            learn_purchase_debit
                            and isinstance(observed_purchase_debit, (int, float))
                            and observed_purchase_debit > 0
                            and float(default_bet) > 0
                        ):
                            learned_multiplier = (
                                float(observed_purchase_debit) / float(default_bet)
                            )
                            purchase["cost_multiplier"] = learned_multiplier
                            purchase["base_source"] = "observed_balance_delta"
                            expected_debit = float(observed_purchase_debit)
                            for discovered in discovered_modes:
                                if discovered.get("id") == mode_id:
                                    discovered["cost_multiplier"] = learned_multiplier
                                    discovered["cost_source"] = "observed_balance_delta"
                                    break
                            progress(
                                f"[{game.name}] {mode_id}: costo aprendido desde "
                                f"balance remoto = x{learned_multiplier:g}."
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
                        _write_json(
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
                            _write_json(
                                attempt_dir / f"step-{wire_steps:03d}-request.json",
                                cont_request,
                            )
                            _write_json(
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
                            _write_json(
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
                        if (
                            mode_id == "SPIN"
                            and active_profile is not None
                            and active_profile.validated != validated
                        ):
                            active_profile.validated = validated
                            persist_profile_snapshot()
                        successes += int(validated)
                        global_warnings.extend(warnings)

                        _write_json(
                            attempt_dir / "remote-proof.json",
                            final_proof,
                        )
                        elapsed_ms = (time.monotonic() - attempt_started) * 1000.0
                        attempts.append(
                            SpinAttempt(
                                number=repetition,
                                ok=validated,
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
                            f"debit={_fmt_number(expected_debit)}, "
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
                        if active_profile is not None and active_profile.family == API_V2:
                            runtime_needs_refresh = True
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
            and not errors
        ):
            # Attempt validation already incorporates fatal protocol warnings.
            # Optional/unclassified actions are coverage metadata, not failures
            # of modes that were actually scheduled and validated.
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
                        f"{mode['name']} x{_fmt_number(mode.get('cost_multiplier'))}"
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

        result = GameTestResult(
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
        session.close()
        return result

