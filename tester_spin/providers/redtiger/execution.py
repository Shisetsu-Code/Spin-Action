from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult, SpinAttempt, utc_now_iso
from tester_spin.providers.base import Progress
from tester_spin.providers.redtiger.bootstrap_browser import bootstrap_game
from tester_spin.providers.redtiger.runtime import (
    FeatureBuy,
    RedTigerRuntime,
    apply_response_token,
    build_spin_payload,
    response_summary,
    sanitize_payload,
    validate_spin_response,
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _mode_id(value: str) -> str:
    clean = "".join(ch if ch.isalnum() else "_" for ch in str(value).upper()).strip("_")
    return clean or "UNKNOWN"


def _sanitize_direct_http_headers(runtime: RedTigerRuntime) -> None:
    browser_only = {
        "connection",
        "content-length",
        "cookie",
        "host",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
    for key in list(runtime.session.headers):
        name = str(key or "")
        lowered = name.casefold()
        if (
            not name
            or name.startswith(":")
            or lowered in browser_only
            or any(ch in name for ch in "\r\n\t ")
        ):
            runtime.session.headers.pop(key, None)


def _post_spin(
    runtime: RedTigerRuntime,
    *,
    stake: Decimal,
    feature_buy: FeatureBuy | None,
    timeout_s: float,
    stop_event,
) -> tuple[int, dict[str, Any], dict[str, Any], list[str]]:
    if stop_event.is_set():
        raise InterruptedError("Detención solicitada antes del spin Red Tiger.")
    payload = build_spin_payload(
        runtime,
        stake=stake,
        feature_buy=feature_buy,
        game_mode=0,
    )
    _sanitize_direct_http_headers(runtime)
    # requests cannot be asynchronously aborted safely from another thread. Keep
    # the only non-cooperative runtime I/O slice short so DETENER never waits the
    # full GUI timeout just because one spin socket is stalled.
    request_timeout = min(5.0, max(1.0, float(timeout_s)))
    response = runtime.session.post(runtime.spin_url, json=payload, timeout=request_timeout)
    if stop_event.is_set():
        raise InterruptedError("Detención solicitada durante el spin Red Tiger.")
    status = int(response.status_code)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Red Tiger spin no devolvió objeto JSON.")
    valid, warnings = validate_spin_response(data)
    if not valid:
        raise ValueError("Red Tiger spin inválido: " + "; ".join(warnings))
    apply_response_token(runtime, data)
    return status, payload, data, warnings


def _persist_runtime_metadata(provider, game: Game, runtime: RedTigerRuntime) -> None:
    path = provider.game_dir(game) / "game.json"
    current: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except Exception:
            current = {}
    settings_game = runtime.settings_response.get("result", {}).get("game", {})
    current.update(
        {
            "provider": provider.key,
            "slug": game.slug,
            "name": game.name,
            "url": game.url,
            "table_id": game.symbol,
            "runtime_game_id": runtime.game_id,
            "runtime_transport": "http_json_platform_game",
            "runtime_bootstrap": "official_demo_route_single_browser_observed_settings",
            "settings_url": runtime.settings_url,
            "spin_url": runtime.spin_url,
            "stakes": [str(value) for value in runtime.stakes],
            "default_stake": str(runtime.default_stake),
            "feature_buys": [
                {"name": feature.name, "multiplier": str(feature.multiplier)}
                for feature in runtime.feature_buys
            ],
            "game_modes": settings_game.get("gameModes") if isinstance(settings_game, dict) else None,
            "math_modes": settings_game.get("mathModes") if isinstance(settings_game, dict) else None,
            "updated_at": utc_now_iso(),
        }
    )
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


class RedTigerExecutionMixin:
    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event,
        progress: Progress,
    ) -> GameTestResult:
        repetitions = max(1, int(spins))
        started_at = utc_now_iso()
        started = time.monotonic()
        run_dir = self.game_dir(game) / "tests" / _timestamp()
        run_dir.mkdir(parents=True, exist_ok=True)

        attempts: list[SpinAttempt] = []
        errors: list[str] = []
        warnings_all: list[str] = []
        coverage_gaps: set[str] = set()
        discovered_modes: list[dict[str, Any]] = []
        successes = 0
        responded = 0
        runtime: RedTigerRuntime | None = None
        cancelled = bool(stop_event.is_set())
        mode_specs: list[tuple[str, str, FeatureBuy | None]] = []

        table_id = self.table_id_for_game(game)
        if not table_id:
            message = "Red Tiger: tableId ausente; el catálogo debe descubrirlo antes de probar."
            return GameTestResult(
                provider=self.key,
                slug=game.slug,
                game_name=game.name,
                game_url=game.url,
                requested_spins=repetitions,
                successful_spins=0,
                failed_spins=repetitions,
                status="ERROR",
                symbol=game.symbol,
                started_at=started_at,
                finished_at=utc_now_iso(),
                elapsed_ms=(time.monotonic() - started) * 1000.0,
                error=message,
                run_dir=str(run_dir),
            )

        try:
            if cancelled:
                raise InterruptedError("Detención solicitada antes de iniciar Red Tiger.")
            progress(f"[{game.name}] Red Tiger: creando demo fresco para tableId={table_id}...")
            runtime = bootstrap_game(
                game.url,
                table_id,
                timeout_s=max(15.0, float(timeout_s)),
                artifact_dir=run_dir / "bootstrap",
                endpoints=self.bootstrap_endpoints,
                progress=progress,
                stop_event=stop_event,
            )
            if stop_event.is_set():
                raise InterruptedError("Detención solicitada al terminar bootstrap Red Tiger.")
            game.symbol = table_id
            _persist_runtime_metadata(self, game, runtime)

            settings_game = runtime.settings_response.get("result", {}).get("game", {})
            discovered_modes.append(
                {
                    "id": "SPIN",
                    "kind": "SPIN",
                    "observed": True,
                    "executable": True,
                    "wire_command": "platform/game/spin",
                    "stake": str(runtime.default_stake),
                    "stakes": [str(value) for value in runtime.stakes],
                }
            )
            mode_specs = [("SPIN", "SPIN", None)]

            for feature in runtime.feature_buys:
                mode_id = f"PURCHASE_{_mode_id(feature.name)}"
                discovered_modes.append(
                    {
                        "id": mode_id,
                        "kind": "PURCHASE",
                        "observed": True,
                        "executable": True,
                        "wire_command": "platform/game/spin",
                        "feature_buy": feature.name,
                        "feature_multiplier": str(feature.multiplier),
                        "stake": str(runtime.default_stake),
                        "cost": str(runtime.default_stake * feature.multiplier),
                    }
                )
                mode_specs.append((mode_id, "PURCHASE", feature))

            has_feature_buy = bool(settings_game.get("hasFeatureBuy")) if isinstance(settings_game, dict) else False
            if has_feature_buy and not runtime.feature_buys:
                coverage_gaps.add("FEATURE_BUY_CONTRACT")

            game_modes = settings_game.get("gameModes") if isinstance(settings_game, dict) else None
            if isinstance(game_modes, list) and game_modes:
                discovered_modes.append(
                    {
                        "id": "GAME_MODES",
                        "kind": "DISCOVERED_ONLY",
                        "observed": True,
                        "executable": False,
                        "values": game_modes,
                    }
                )
                coverage_gaps.add("GAME_MODES_WIRE_CONTRACT")

            progress(
                f"[{game.name}] Red Tiger SETTINGS OK: gameId={runtime.game_id}, "
                f"stakes={len(runtime.stakes)}, default={runtime.default_stake}, "
                f"compras={len(runtime.feature_buys)}, endpoint={runtime.spin_url}."
            )
        except InterruptedError as exc:
            cancelled = True
            progress(f"[{game.name}] Red Tiger CANCELADO: {exc}")
            mode_specs = []
        except Exception as exc:
            if stop_event.is_set():
                cancelled = True
                progress(f"[{game.name}] Red Tiger CANCELADO durante cierre: {type(exc).__name__}: {exc}")
            else:
                message = f"{type(exc).__name__}: {exc}"
                errors.append(message)
                progress(f"[{game.name}] Red Tiger bootstrap/settings ERROR: {message}")
            mode_specs = []

        requested_total = repetitions * len(mode_specs) if mode_specs else repetitions

        if runtime is not None and mode_specs and not cancelled:
            try:
                for mode_id, mode_kind, feature in mode_specs:
                    if cancelled:
                        break
                    for repetition in range(1, repetitions + 1):
                        if stop_event.is_set():
                            cancelled = True
                            break
                        attempt_dir = run_dir / mode_id / f"attempt-{repetition:05d}"
                        attempt_dir.mkdir(parents=True, exist_ok=True)
                        attempt_started = time.monotonic()
                        try:
                            status_code, request_payload, response_payload, warnings = _post_spin(
                                runtime,
                                stake=runtime.default_stake,
                                feature_buy=feature,
                                timeout_s=timeout_s,
                                stop_event=stop_event,
                            )
                            responded += 1
                            summary = response_summary(response_payload)
                            _write_json(attempt_dir / "request.json", sanitize_payload(request_payload))
                            _write_json(attempt_dir / "response.json", sanitize_payload(response_payload))
                            _write_json(attempt_dir / "summary.json", summary)

                            terminal = bool(summary.get("success"))
                            validated = terminal and not warnings
                            successes += int(validated)
                            warnings_all.extend(warnings)
                            elapsed_ms = (time.monotonic() - attempt_started) * 1000.0
                            attempts.append(
                                SpinAttempt(
                                    number=repetition,
                                    ok=validated,
                                    mode_id=mode_id,
                                    mode_kind=mode_kind,
                                    status_code=status_code,
                                    elapsed_ms=elapsed_ms,
                                    symbol=runtime.game_id,
                                    endpoint=runtime.spin_url,
                                    na=",".join(summary.get("spin_modes") or []),
                                    terminal=terminal,
                                    wire_steps=1,
                                    warning="; ".join(warnings),
                                    artifact_dir=str(attempt_dir),
                                )
                            )
                            progress(
                                f"[{game.name}] {mode_id} {repetition}/{repetitions}: "
                                f"{'OK' if validated else 'PARCIAL'} {elapsed_ms:.0f} ms, "
                                f"spinModes={summary.get('spin_modes') or ['—']}."
                            )
                        except InterruptedError as exc:
                            cancelled = True
                            progress(f"[{game.name}] {mode_id} CANCELADO: {exc}")
                            break
                        except Exception as exc:
                            if stop_event.is_set():
                                cancelled = True
                                progress(
                                    f"[{game.name}] {mode_id} CANCELADO durante I/O: "
                                    f"{type(exc).__name__}: {exc}"
                                )
                                break
                            message = f"{type(exc).__name__}: {exc}"
                            errors.append(message)
                            elapsed_ms = (time.monotonic() - attempt_started) * 1000.0
                            attempts.append(
                                SpinAttempt(
                                    number=repetition,
                                    ok=False,
                                    mode_id=mode_id,
                                    mode_kind=mode_kind,
                                    elapsed_ms=elapsed_ms,
                                    symbol=runtime.game_id,
                                    endpoint=runtime.spin_url,
                                    terminal=False,
                                    wire_steps=0,
                                    error=message,
                                    artifact_dir=str(attempt_dir),
                                )
                            )
                            progress(
                                f"[{game.name}] {mode_id} {repetition}/{repetitions}: ERROR {message}"
                            )
            finally:
                runtime.session.close()

        elapsed_total = (time.monotonic() - started) * 1000.0
        if cancelled or stop_event.is_set():
            status = "CANCELADO"
            error = "Detención solicitada por el usuario."
        elif requested_total and successes == requested_total and not errors and not coverage_gaps:
            status = "OK"
            error = ""
        elif responded:
            status = "PARCIAL"
            parts = [
                f"Red Tiger respondió {responded}/{requested_total}; validados={successes}/{requested_total}."
            ]
            if coverage_gaps:
                parts.append("Cobertura pendiente: " + ", ".join(sorted(coverage_gaps)) + ".")
            if warnings_all:
                parts.append("Diagnóstico: " + " | ".join(list(dict.fromkeys(warnings_all))[:6]))
            if errors:
                parts.append("Errores: " + " | ".join(list(dict.fromkeys(errors))[:4]))
            error = " ".join(parts)
        else:
            status = "ERROR"
            error = errors[0] if errors else "Red Tiger no completó ninguna acción."

        result = GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=requested_total,
            successful_spins=successes,
            failed_spins=max(0, requested_total - successes),
            status=status,
            symbol=(runtime.game_id if runtime is not None else game.symbol),
            discovered_modes=discovered_modes,
            started_at=started_at,
            finished_at=utc_now_iso(),
            elapsed_ms=elapsed_total,
            error=error,
            run_dir=str(run_dir),
            attempts=attempts,
        )
        _write_json(run_dir / "result.json", result.to_dict())
        return result
