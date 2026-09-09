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
from tester_spin.providers.base import Progress
from tester_spin.providers.bgaming.runtime import (
    BGamingRuntime,
    balance_total,
    flow_continuation_command,
    post_command,
    response_fingerprint,
    resolve_base_bet,
    sanitize_error_text,
    sanitize_options,
    sanitize_session_url,
    validate_init,
    validate_spin,
)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _download_text(
    session: requests.Session,
    url: str,
    *,
    timeout_s: float,
) -> str:
    response = session.get(url, timeout=timeout_s)
    response.raise_for_status()
    return response.text


def _bundle_candidates(
    runtime: BGamingRuntime,
    *,
    timeout_s: float,
) -> list[str]:
    candidates: list[str] = []
    resources_path = str(runtime.options.get("resources_path") or "").rstrip("/")
    loader_url = str(runtime.options.get("games_loader_source") or "").strip()

    if loader_url:
        try:
            loader = _download_text(
                runtime.session,
                loader_url,
                timeout_s=timeout_s,
            )
        except Exception:
            loader = ""
        if loader:
            patterns = [
                r'res:\w+="([^"]+)"',
                r"res:\w+='([^']+)'",
                r'res:"([^"]+)"',
                r"res:'([^']+)'",
            ]
            for pattern in patterns:
                match = re.search(pattern, loader)
                if match and resources_path:
                    candidates.append(
                        f"{resources_path}/{match.group(1)}/bundle.js"
                    )
                    break

    configured = str(runtime.options.get("game_bundle_source") or "").strip()
    if configured:
        candidates.append(configured)

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped


def discover_switchable_identifiers(
    runtime: BGamingRuntime,
    *,
    timeout_s: float,
) -> list[str]:
    base = runtime.identifier
    if not base:
        return []

    bundle = ""
    for url in _bundle_candidates(runtime, timeout_s=timeout_s):
        try:
            text = _download_text(
                runtime.session,
                url,
                timeout_s=timeout_s,
            )
        except Exception:
            continue
        if text:
            bundle = text
            if f'"{base}' in text or f"'{base}" in text:
                break

    if not bundle:
        return []

    pattern = re.compile(
        rf'["\']({re.escape(base)}[A-Za-z0-9_]+)["\']'
    )
    identifiers: list[str] = []
    seen: set[str] = set()
    for match in pattern.finditer(bundle):
        identifier = match.group(1)
        suffix = identifier[len(base):]
        if not suffix or identifier in seen:
            continue
        # Container variants observed so far are selectable numbered modes.
        # Restricting to a suffix containing a digit avoids class/telemetry names.
        if not any(ch.isdigit() for ch in suffix):
            continue
        seen.add(identifier)
        identifiers.append(identifier)

    # Preserve the explicit order from the client bundle, which is also the UI order.
    return identifiers


def switch_runtime(
    runtime: BGamingRuntime,
    target_identifier: str,
    *,
    timeout_s: float,
) -> BGamingRuntime:
    lobby_url = str(runtime.options.get("lobby_launch_url") or "").strip()
    if not lobby_url:
        raise ValueError("BGaming container sin lobby_launch_url.")

    response = runtime.session.get(
        lobby_url,
        params={
            "game": target_identifier,
            "from": runtime.identifier,
        },
        allow_redirects=True,
        timeout=timeout_s,
        headers={
            "Accept": "application/json",
            "Referer": runtime.launch_url,
        },
    )
    response.raise_for_status()
    try:
        options = response.json()
    except ValueError as exc:
        raise ValueError(
            "BGaming container: el cambio de variante no devolvió JSON."
        ) from exc
    if not isinstance(options, dict):
        raise ValueError(
            "BGaming container: respuesta de cambio de variante inesperada."
        )

    identifier = str(options.get("identifier") or "").strip()
    api_url = str(options.get("api") or "").strip()
    csrf_name = str(options.get("csrfTokenHeaderName") or "").strip()
    csrf_value = str(options.get("csrfTokenHeaderValue") or "").strip()
    if identifier != target_identifier:
        raise ValueError(
            f"BGaming container cambió a {identifier!r}, "
            f"esperado {target_identifier!r}."
        )
    if not api_url or not csrf_name or not csrf_value:
        raise ValueError(
            "BGaming container: variante sin api/CSRF utilizables."
        )

    launch_url = str(options.get("game_page_url") or response.url)
    return BGamingRuntime(
        session=runtime.session,
        launch_url=launch_url,
        api_url=api_url,
        identifier=identifier,
        csrf_header_name=csrf_name,
        csrf_header_value=csrf_value,
        options=options,
        round_series_id=runtime.round_series_id,
    )


def _validate_close(
    data: dict[str, Any],
    *,
    previous_balance_total: int | float | None,
) -> list[str]:
    warnings: list[str] = []
    flow = data.get("flow")
    if not isinstance(flow, dict):
        return ["close sin flow"]
    if str(flow.get("command") or "") != "close":
        warnings.append(f"close flow.command={flow.get('command')!r}")
    if str(flow.get("state") or "") != "closed":
        warnings.append(f"close flow.state={flow.get('state')!r}")
    actions = flow.get("available_actions")
    if isinstance(actions, list) and "spin" not in {str(x) for x in actions}:
        warnings.append(f"close sin spin disponible: {actions!r}")

    current_total = balance_total(data)
    if (
        previous_balance_total is not None
        and current_total is not None
        and abs(float(current_total) - float(previous_balance_total)) > 1e-9
    ):
        warnings.append(
            f"close alteró balance: antes={previous_balance_total}, "
            f"después={current_total}"
        )
    return warnings


def run_switchable_container_test(
    *,
    game: Game,
    runtime: BGamingRuntime,
    initial_data: dict[str, Any],
    spins: int,
    timeout_s: float,
    stop_event: threading.Event,
    progress: Progress,
    run_dir: Path,
    started_iso: str,
    started_monotonic: float,
) -> GameTestResult:
    identifiers = discover_switchable_identifiers(
        runtime,
        timeout_s=timeout_s,
    )
    if not identifiers:
        elapsed = (time.monotonic() - started_monotonic) * 1000.0
        return GameTestResult(
            provider="bgaming",
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=max(1, int(spins)),
            successful_spins=0,
            failed_spins=max(1, int(spins)),
            status="PARCIAL",
            symbol=runtime.identifier,
            discovered_modes=[],
            started_at=started_iso,
            finished_at=utc_now_iso(),
            elapsed_ms=elapsed,
            error=(
                "BGaming container detectado, pero no se pudieron descubrir "
                "variantes apostables en el bundle."
            ),
            run_dir=str(run_dir),
            attempts=[],
        )

    run_dir = run_dir.parent / run_dir.name.replace(
        "bgaming-http-api-v2",
        "bgaming-switchable-container",
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    repetitions = max(1, int(spins))
    requested_total = len(identifiers) * repetitions
    successes = 0
    responded = 0
    attempts: list[SpinAttempt] = []
    errors: list[str] = []
    discovered_modes = [
        {
            "id": f"VARIANT_{identifier.upper()}",
            "kind": "VARIANT",
            "observed": True,
            "wire_command": "lobby_switch+init+spin",
            "identifier": identifier,
        }
        for identifier in identifiers
    ]

    progress(
        f"[{game.name}] contenedor BGaming detectado: "
        f"{len(identifiers)} variantes → {', '.join(identifiers)}."
    )

    current_runtime = runtime
    current_total = balance_total(initial_data)

    for identifier in identifiers:
        if stop_event.is_set():
            break

        mode_id = f"VARIANT_{identifier.upper()}"
        variant_dir = run_dir / mode_id
        try:
            child_runtime = switch_runtime(
                current_runtime,
                identifier,
                timeout_s=timeout_s,
            )
            _write_json(
                variant_dir / "switch.json",
                {
                    "identifier": child_runtime.identifier,
                    "launch_url": sanitize_session_url(child_runtime.launch_url),
                    "api_url": sanitize_session_url(child_runtime.api_url),
                    "options": sanitize_options(child_runtime.options),
                },
            )
            _init_response, init_request, init_data = post_command(
                child_runtime,
                "init",
                timeout_s=timeout_s,
            )
            _write_json(variant_dir / "init-request.json", init_request)
            _write_json(variant_dir / "init-response.json", init_data)

            init_warnings = validate_init(init_data)
            bet, bet_source = resolve_base_bet(init_data)
            if not isinstance(bet, (int, float)):
                raise ValueError(
                    f"{identifier}: init sin apuesta utilizable."
                )

            options = init_data.get("options")
            expected_reels: int | None = None
            expected_rows: int | None = None
            if isinstance(options, dict):
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

            init_total = balance_total(init_data)
            if init_total is not None:
                current_total = init_total

            progress(
                f"[{game.name}] {mode_id} INIT OK: bet={bet} "
                f"({bet_source}), layout={expected_reels or '?'}x"
                f"{expected_rows or '?'}, balance={current_total}."
            )
            current_runtime = child_runtime

            for repetition in range(1, repetitions + 1):
                if stop_event.is_set():
                    break
                attempt_dir = variant_dir / f"attempt-{repetition:03d}"
                attempt_started = time.monotonic()
                warnings = list(init_warnings)
                steps = 0
                last_status: int | None = None
                terminal = False
                try:
                    before_total = current_total
                    response, request_payload, data = post_command(
                        child_runtime,
                        "spin",
                        timeout_s=timeout_s,
                        options={"bet": bet},
                    )
                    responded += 1
                    steps = 1
                    last_status = int(response.status_code)
                    _write_json(
                        attempt_dir / "step-001-request.json",
                        request_payload,
                    )
                    _write_json(
                        attempt_dir / "step-001-response.json",
                        data,
                    )

                    warnings.extend(
                        validate_spin(
                            data,
                            requested_bet=bet,
                            previous_balance_total=before_total,
                            expected_reels=expected_reels,
                            expected_rows=expected_rows,
                            command="spin",
                            expected_debit=bet,
                        )
                    )
                    total = balance_total(data)
                    if total is not None:
                        current_total = total

                    flow = data.get("flow")
                    if not isinstance(flow, dict):
                        flow = {}

                    guard = 256
                    while not stop_event.is_set():
                        command = flow_continuation_command({"flow": flow})
                        if not command:
                            break
                        if steps >= guard:
                            warnings.append(
                                f"guard de continuaciones alcanzado ({guard})"
                            )
                            break

                        before_continuation = current_total
                        cont_response, cont_request, cont_data = post_command(
                            child_runtime,
                            command,
                            timeout_s=timeout_s,
                        )
                        steps += 1
                        last_status = int(cont_response.status_code)
                        _write_json(
                            attempt_dir / f"step-{steps:03d}-request.json",
                            cont_request,
                        )
                        _write_json(
                            attempt_dir / f"step-{steps:03d}-response.json",
                            cont_data,
                        )

                        if command == "close":
                            warnings.extend(
                                _validate_close(
                                    cont_data,
                                    previous_balance_total=before_continuation,
                                )
                            )
                        else:
                            warnings.extend(
                                validate_spin(
                                    cont_data,
                                    requested_bet=bet,
                                    previous_balance_total=before_continuation,
                                    expected_reels=expected_reels,
                                    expected_rows=expected_rows,
                                    command=command,
                                    expected_debit=0,
                                )
                            )

                        total = balance_total(cont_data)
                        if total is not None:
                            current_total = total
                        flow = cont_data.get("flow")
                        if not isinstance(flow, dict):
                            flow = {}

                    actions = flow.get("available_actions")
                    action_names = (
                        {str(item) for item in actions}
                        if isinstance(actions, list)
                        else set()
                    )
                    terminal = (
                        str(flow.get("state") or "") == "closed"
                        and "spin" in action_names
                    )
                    if not terminal:
                        warnings.append(
                            f"variante no terminal: state={flow.get('state')!r}, "
                            f"actions={sorted(action_names)!r}"
                        )

                    validated = terminal and not warnings
                    successes += int(validated)
                    proof = {
                        "runtime": "switchable-container",
                        "identifier": identifier,
                        "mode_id": mode_id,
                        "steps": steps,
                        "terminal": terminal,
                        "balance_total": current_total,
                        "flow_state": flow.get("state"),
                        "flow_command": flow.get("command"),
                        "response_sha256": response_fingerprint(data),
                    }
                    _write_json(attempt_dir / "remote-proof.json", proof)

                    elapsed_ms = (
                        time.monotonic() - attempt_started
                    ) * 1000.0
                    attempts.append(
                        SpinAttempt(
                            number=repetition,
                            ok=True,
                            mode_id=mode_id,
                            mode_kind="VARIANT",
                            status_code=last_status,
                            elapsed_ms=elapsed_ms,
                            symbol=identifier,
                            endpoint=sanitize_session_url(child_runtime.api_url),
                            na=str(flow.get("state") or ""),
                            terminal=terminal,
                            wire_steps=steps,
                            warning="; ".join(warnings),
                            artifact_dir=str(attempt_dir),
                        )
                    )
                    progress(
                        f"[{game.name}] {mode_id} {repetition}/{repetitions}: "
                        f"{'OK' if validated else 'PARCIAL'} "
                        f"{elapsed_ms:.0f} ms, steps={steps}, "
                        f"HTTP={last_status or '—'}, balance={current_total}"
                        + (
                            f", diagnóstico={' | '.join(warnings[:3])}"
                            if warnings
                            else ""
                        )
                    )
                except Exception as exc:
                    message = sanitize_error_text(
                        f"{type(exc).__name__}: {exc}"
                    )
                    errors.append(message)
                    elapsed_ms = (
                        time.monotonic() - attempt_started
                    ) * 1000.0
                    attempts.append(
                        SpinAttempt(
                            number=repetition,
                            ok=False,
                            mode_id=mode_id,
                            mode_kind="VARIANT",
                            elapsed_ms=elapsed_ms,
                            symbol=identifier,
                            endpoint=sanitize_session_url(child_runtime.api_url),
                            terminal=False,
                            wire_steps=steps,
                            error=message,
                            artifact_dir=str(attempt_dir),
                        )
                    )
                    progress(
                        f"[{game.name}] {mode_id} {repetition}/{repetitions}: "
                        f"ERROR {message}"
                    )
        except Exception as exc:
            message = sanitize_error_text(f"{type(exc).__name__}: {exc}")
            errors.append(message)
            for repetition in range(1, repetitions + 1):
                attempts.append(
                    SpinAttempt(
                        number=repetition,
                        ok=False,
                        mode_id=mode_id,
                        mode_kind="VARIANT",
                        symbol=identifier,
                        terminal=False,
                        error=message,
                        artifact_dir=str(
                            variant_dir / f"attempt-{repetition:03d}"
                        ),
                    )
                )
            progress(f"[{game.name}] {mode_id}: ERROR {message}")

    elapsed = (time.monotonic() - started_monotonic) * 1000.0
    if successes == requested_total and len(attempts) == requested_total:
        status = "OK"
        error = ""
    elif responded:
        status = "PARCIAL"
        error = (
            f"BGaming container respondió {responded}/{requested_total}; "
            f"variantes validadas={successes}/{requested_total}."
        )
        if errors:
            error += " Errores: " + " | ".join(errors[:3])
    else:
        status = "ERROR"
        error = errors[0] if errors else "BGaming container sin respuestas."

    return GameTestResult(
        provider="bgaming",
        slug=game.slug,
        game_name=game.name,
        game_url=game.url,
        requested_spins=requested_total,
        successful_spins=successes,
        failed_spins=max(0, requested_total - successes),
        status=status,
        symbol=runtime.identifier,
        discovered_modes=discovered_modes,
        started_at=started_iso,
        finished_at=utc_now_iso(),
        elapsed_ms=elapsed,
        error=error,
        run_dir=str(run_dir),
        attempts=attempts,
    )
