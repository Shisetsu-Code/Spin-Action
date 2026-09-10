from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult, SpinAttempt, utc_now_iso
from tester_spin.providers.base import Progress
from tester_spin.providers.rubyplay.runtime import (
    RubyPlayClientProfile,
    RubyPlayRuntime,
    bootstrap_game,
    post_action,
    purchase_price,
    sanitize_request_payload,
    validate_action_response,
)


SAFE_CONTINUATIONS = {"respin"}
CONTINUATION_GUARD = 256


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _mode_id(value: str) -> str:
    clean = "".join(ch if ch.isalnum() else "_" for ch in value.upper()).strip("_")
    return clean or "UNKNOWN"


def _persist_protocol_metadata(provider, game: Game, runtime: RubyPlayRuntime) -> None:
    path = provider.game_dir(game) / "game.json"
    current: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except Exception:
            current = {}
    current.update(
        {
            "provider": provider.key,
            "slug": game.slug,
            "name": game.name,
            "url": game.url,
            "identifier": runtime.launcher.gamename,
            "launcher": {
                "url": runtime.launcher.launcher_url,
                "gamename": runtime.launcher.gamename,
                "operator": runtime.launcher.operator,
                "server_url": runtime.launcher.server_url,
                "currency": runtime.launcher.currency,
                "mode": runtime.launcher.mode,
                "lang": runtime.launcher.lang,
            },
            "client_profile": runtime.client_profile.to_dict(),
            "bet_profile": runtime.bet_plan.to_dict(),
            "runtime_transport": "http_json_gameserver",
            "state_authority": "response.data.next_action",
            "state_carrier": "funModeData",
            "updated_at": utc_now_iso(),
        }
    )
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


class RubyPlayExecutionMixin:
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
        started_iso = utc_now_iso()
        started = time.monotonic()
        run_dir = self.game_dir(game) / "tests" / _safe_timestamp()
        run_dir.mkdir(parents=True, exist_ok=True)

        attempts: list[SpinAttempt] = []
        errors: list[str] = []
        global_warnings: list[str] = []
        coverage_gaps: set[str] = set()
        discovered_modes: list[dict[str, Any]] = []
        responded_attempts = 0
        successes = 0

        session = self._new_session()
        runtime: RubyPlayRuntime | None = None
        profile: RubyPlayClientProfile | None = None
        mode_specs: list[dict[str, Any]] = []

        try:
            runtime = bootstrap_game(
                session,
                game.url,
                timeout_s=timeout_s,
                artifact_dir=run_dir / "bootstrap",
            )
            profile = runtime.client_profile
            game.symbol = runtime.launcher.gamename
            _persist_protocol_metadata(self, game, runtime)

            discovered_modes.append(
                {
                    "id": "SPIN",
                    "kind": "SPIN",
                    "observed": True,
                    "executable": True,
                    "wire_command": "spin",
                    "bet_policy": "default",
                    "allowed_bets": list(runtime.bets),
                    "default_bet": runtime.default_bet,
                    "wager": profile.wager,
                    "effective_stake": runtime.bet_plan.effective_stake,
                }
            )
            mode_specs.append({"id": "SPIN", "kind": "SPIN"})

            init_body = runtime.init_data.get("data")
            buy_available = bool(
                isinstance(init_body, dict)
                and init_body.get("buy_feature_available") is True
            )
            if buy_available:
                executable = bool(
                    profile.buy_feature_type
                    and isinstance(profile.wager, (int, float))
                    and profile.wager > 0
                    and isinstance(profile.buy_feature_multiplier, (int, float))
                    and profile.buy_feature_multiplier > 0
                )
                feature_name = profile.buy_feature_type or "unknown"
                purchase_id = (
                    f"PURCHASE_{_mode_id(feature_name)}"
                    if feature_name != "unknown"
                    else "BUY_FEATURE"
                )
                price = purchase_price(
                    runtime.default_bet,
                    wager=profile.wager,
                    multiplier=profile.buy_feature_multiplier,
                )
                discovered_modes.append(
                    {
                        "id": purchase_id,
                        "kind": "PURCHASE",
                        "observed": True,
                        "client_observed": bool(profile.buy_feature_type),
                        "executable": executable,
                        "wire_command": "buy_feature",
                        "buy_feature_type": profile.buy_feature_type,
                        "pricing_basis": "bet*wager*feature_multiplier",
                        "feature_multiplier": profile.buy_feature_multiplier,
                        "default_price": price,
                        "bet_policy": "default",
                    }
                )
                if executable:
                    mode_specs.append(
                        {
                            "id": purchase_id,
                            "kind": "PURCHASE",
                            "feature_type": profile.buy_feature_type,
                        }
                    )
                else:
                    coverage_gaps.add("BUY_FEATURE_CONTRACT")

            progress(
                f"[{game.name}] RubyPlay INIT OK: game={runtime.launcher.gamename}, "
                f"protocol={profile.protocol_version}, math={profile.math_version}, "
                f"bets={len(runtime.bets)}, default={runtime.default_bet}, "
                f"wager={profile.wager if profile.wager is not None else '—'}, "
                f"next={runtime.next_action}, modos={len(mode_specs)}."
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            errors.append(message)
            progress(f"[{game.name}] RubyPlay bootstrap/init ERROR: {message}")

        requested_total = repetitions * len(mode_specs)

        def fresh_runtime(mode_id: str) -> RubyPlayRuntime:
            fresh_session = self._new_session()
            try:
                value = bootstrap_game(
                    fresh_session,
                    game.url,
                    timeout_s=timeout_s,
                    cached_profile=profile,
                    artifact_dir=run_dir / mode_id / "session-refresh",
                )
                return value
            except Exception:
                fresh_session.close()
                raise

        if runtime is not None and mode_specs:
            current_runtime = runtime
            for mode_index, mode in enumerate(mode_specs):
                if stop_event.is_set():
                    break
                mode_id = str(mode["id"])
                mode_kind = str(mode["kind"])
                if mode_index > 0:
                    current_runtime.session.close()
                    current_runtime = fresh_runtime(mode_id)

                runtime_needs_refresh = False
                for repetition in range(1, repetitions + 1):
                    if stop_event.is_set():
                        break
                    if runtime_needs_refresh:
                        current_runtime.session.close()
                        current_runtime = fresh_runtime(mode_id)
                        runtime_needs_refresh = False

                    attempt_dir = run_dir / mode_id / f"attempt-{repetition:05d}"
                    attempt_dir.mkdir(parents=True, exist_ok=True)
                    attempt_started = time.monotonic()
                    warnings: list[str] = []
                    wire_steps = 0
                    last_status_code: int | None = None
                    terminal = False

                    try:
                        if mode_kind == "PURCHASE":
                            feature_type = str(mode.get("feature_type") or "")
                            price = purchase_price(
                                current_runtime.default_bet,
                                wager=current_runtime.client_profile.wager,
                                multiplier=current_runtime.client_profile.buy_feature_multiplier,
                            )
                            response, request_payload, data, previous_an = post_action(
                                current_runtime,
                                "buy_feature",
                                timeout_s=timeout_s,
                                bet=current_runtime.default_bet,
                                buy_feature_type=feature_type,
                                buy_feature_price=price,
                            )
                            expected_price = price
                        else:
                            response, request_payload, data, previous_an = post_action(
                                current_runtime,
                                "spin",
                                timeout_s=timeout_s,
                                bet=current_runtime.default_bet,
                            )
                            expected_price = None

                        responded_attempts += 1
                        wire_steps += 1
                        last_status_code = int(response.status_code)
                        _write_json(attempt_dir / "request.json", sanitize_request_payload(request_payload))
                        _write_json(attempt_dir / "response.json", data)
                        warnings.extend(
                            validate_action_response(
                                data,
                                action=str(request_payload["action"]),
                                previous_an=previous_an,
                            )
                        )

                        if mode_kind == "PURCHASE":
                            body = data.get("data") if isinstance(data.get("data"), dict) else {}
                            returned_type = str(body.get("buy_feature_type") or "")
                            if returned_type and returned_type != str(mode.get("feature_type") or ""):
                                warnings.append(
                                    f"buy_feature_type={returned_type!r}, solicitado={mode.get('feature_type')!r}"
                                )
                            returned_price = body.get("buy_feature_price")
                            if (
                                expected_price is not None
                                and isinstance(returned_price, (int, float))
                                and float(returned_price) != float(expected_price)
                            ):
                                warnings.append(
                                    f"buy_feature_price={returned_price!r}, esperado={expected_price!r}"
                                )

                        while not stop_event.is_set() and current_runtime.next_action != "spin":
                            action = current_runtime.next_action
                            discovered_id = f"CONTINUATION_{_mode_id(action)}"
                            if not any(item.get("id") == discovered_id for item in discovered_modes):
                                discovered_modes.append(
                                    {
                                        "id": discovered_id,
                                        "kind": "CONTINUATION",
                                        "observed": True,
                                        "executable": action in SAFE_CONTINUATIONS,
                                        "wire_command": action,
                                    }
                                )
                            if action not in SAFE_CONTINUATIONS:
                                coverage_gaps.add(discovered_id)
                                warnings.append(
                                    f"continuación {action!r} observada pero wire-shape no demostrado"
                                )
                                runtime_needs_refresh = True
                                break
                            if wire_steps >= CONTINUATION_GUARD:
                                warnings.append(
                                    f"guard de continuaciones alcanzado ({CONTINUATION_GUARD})"
                                )
                                runtime_needs_refresh = True
                                break

                            response, request_payload, data, previous_an = post_action(
                                current_runtime,
                                action,
                                timeout_s=timeout_s,
                            )
                            wire_steps += 1
                            last_status_code = int(response.status_code)
                            _write_json(
                                attempt_dir / f"step-{wire_steps:03d}-request.json",
                                sanitize_request_payload(request_payload),
                            )
                            _write_json(
                                attempt_dir / f"step-{wire_steps:03d}-response.json",
                                data,
                            )
                            warnings.extend(
                                validate_action_response(
                                    data,
                                    action=action,
                                    previous_an=previous_an,
                                )
                            )

                        terminal = current_runtime.next_action == "spin"
                        if not terminal and not stop_event.is_set():
                            warnings.append(
                                f"ronda no terminal: next_action={current_runtime.next_action!r}"
                            )
                        validated = terminal and not warnings
                        successes += int(validated)
                        global_warnings.extend(warnings)
                        elapsed_ms = (time.monotonic() - attempt_started) * 1000.0
                        attempts.append(
                            SpinAttempt(
                                number=repetition,
                                ok=validated,
                                mode_id=mode_id,
                                mode_kind=mode_kind,
                                status_code=last_status_code,
                                elapsed_ms=elapsed_ms,
                                symbol=current_runtime.launcher.gamename,
                                endpoint=current_runtime.gameserver_url,
                                na=current_runtime.next_action,
                                terminal=terminal,
                                wire_steps=wire_steps,
                                warning="; ".join(warnings),
                                artifact_dir=str(attempt_dir),
                            )
                        )
                        progress(
                            f"[{game.name}] {mode_id} {repetition}/{repetitions}: "
                            f"{'OK' if validated else 'PARCIAL'} {elapsed_ms:.0f} ms, "
                            f"steps={wire_steps}, bet={current_runtime.default_bet}, "
                            f"next={current_runtime.next_action}."
                        )
                    except Exception as exc:
                        runtime_needs_refresh = True
                        elapsed_ms = (time.monotonic() - attempt_started) * 1000.0
                        message = f"{type(exc).__name__}: {exc}"
                        errors.append(message)
                        attempts.append(
                            SpinAttempt(
                                number=repetition,
                                ok=False,
                                mode_id=mode_id,
                                mode_kind=mode_kind,
                                status_code=last_status_code,
                                elapsed_ms=elapsed_ms,
                                symbol=current_runtime.launcher.gamename,
                                endpoint=current_runtime.gameserver_url,
                                terminal=False,
                                wire_steps=wire_steps,
                                error=message,
                                artifact_dir=str(attempt_dir),
                            )
                        )
                        progress(
                            f"[{game.name}] {mode_id} {repetition}/{repetitions}: ERROR {message}"
                        )

            current_runtime.session.close()
        else:
            session.close()

        elapsed_total = (time.monotonic() - started) * 1000.0
        if requested_total and successes == requested_total and not errors and not coverage_gaps:
            status = "OK"
            error = ""
        elif responded_attempts:
            status = "PARCIAL"
            parts = [
                f"RubyPlay respondió {responded_attempts}/{requested_total} intentos; "
                f"rondas terminales validadas={successes}/{requested_total}."
            ]
            if coverage_gaps:
                parts.append("Cobertura pendiente: " + ", ".join(sorted(coverage_gaps)) + ".")
            if global_warnings:
                parts.append("Diagnóstico: " + " | ".join(list(dict.fromkeys(global_warnings))[:6]))
            error = " ".join(parts)
        else:
            status = "ERROR"
            error = errors[0] if errors else "RubyPlay no completó ninguna iteración."

        total_expected = requested_total or repetitions
        return GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=total_expected,
            successful_spins=successes,
            failed_spins=max(0, total_expected - successes),
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
