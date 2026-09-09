from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from tester_spin.models import Game, GameTestResult, SpinAttempt, utc_now_iso
from tester_spin.providers.base import Progress
from tester_spin.providers.bgaming.runtime import (
    BGamingRuntime,
    response_fingerprint,
    sanitize_error_text,
    sanitize_session_url,
)


def is_hyperhive_runtime(runtime: BGamingRuntime) -> bool:
    # game_bundle_source/version also exist in normal API-v2 launches.
    # The HAR-confirmed discriminator is the final /hyperhive route.
    path = urlparse(runtime.launch_url).path.rstrip("/").casefold()
    return path.endswith("/hyperhive")


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_lower = str(key).casefold()
            if "token" in key_lower or key_lower in {"state_lock"}:
                out[key] = "<redacted>"
            elif isinstance(item, str) and (
                "rounds_history/" in item
                or "launch_token=" in item
                or "play_token=" in item
            ):
                out[key] = sanitize_session_url(item)
            else:
                out[key] = _safe_json(item)
        return out
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return value


def _rpc(
    runtime: BGamingRuntime,
    method: str,
    *,
    timeout_s: float,
    params: dict[str, Any],
    rpc_id: str | None = None,
) -> tuple[requests.Response, dict[str, Any], dict[str, Any]]:
    api_url = _origin(runtime.launch_url) + "/api"
    payload = {
        "id": rpc_id or str(uuid.uuid4()),
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": _origin(runtime.launch_url),
        "Referer": runtime.launch_url,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    response = runtime.session.post(
        api_url,
        json=payload,
        headers=headers,
        timeout=timeout_s,
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError("BGaming HyperHive: respuesta JSON-RPC no JSON.") from exc
    if not isinstance(data, dict):
        raise ValueError("BGaming HyperHive: respuesta JSON-RPC inesperada.")
    if data.get("error") is not None:
        raise ValueError(f"BGaming HyperHive RPC error: {data.get('error')!r}")
    if not isinstance(data.get("result"), dict):
        raise ValueError("BGaming HyperHive: respuesta sin result.")
    return response, payload, data


def _download_bundle(runtime: BGamingRuntime, timeout_s: float) -> str:
    url = str(runtime.options.get("game_bundle_source") or "").strip()
    if not url:
        return ""
    try:
        response = runtime.session.get(url, timeout=timeout_s)
        response.raise_for_status()
        return response.text
    except Exception:
        return ""


def discover_modes_from_bundle(
    runtime: BGamingRuntime,
    *,
    timeout_s: float,
) -> list[dict[str, Any]]:
    """Discover the exact HyperHive request vocabulary from the loaded bundle."""
    bundle = _download_bundle(runtime, timeout_s)
    bet_type = "betting" if 'bet_type:"betting"' in bundle else "bet"

    spin_request: dict[str, Any] = {"bet_type": bet_type}
    if 'action:"spin"' in bundle:
        spin_request["action"] = "spin"

    modes: list[dict[str, Any]] = [
        {
            "id": "SPIN",
            "kind": "SPIN",
            "request": spin_request,
            "expected_multiplier": 1.0,
            "source": "game_bundle_source" if bundle else "hyperhive-base",
        }
    ]
    if not bundle:
        return modes

    if 'purchased_feature:"buy_chance"' in bundle:
        modes.append(
            {
                "id": "PURCHASE_BUY_CHANCE",
                "kind": "PURCHASE",
                "request": {
                    "purchased_feature": "buy_chance",
                    "bet_type": bet_type,
                },
                "expected_multiplier": None,
                "source": "game_bundle_source",
            }
        )

    variants_found = False
    for variant, mode_id in [
        ("freeSpin", "PURCHASE_BUY_BONUS_FREESPIN"),
        ("freeSpinRandom", "PURCHASE_BUY_BONUS_RANDOM"),
    ]:
        literal = (
            'purchased_feature:"buy_bonus",bonus_multiplier_type:"'
            + variant
            + '"'
        )
        if literal in bundle:
            variants_found = True
            modes.append(
                {
                    "id": mode_id,
                    "kind": "PURCHASE",
                    "request": {
                        "purchased_feature": "buy_bonus",
                        "bonus_multiplier_type": variant,
                        "bet_type": bet_type,
                    },
                    "expected_multiplier": None,
                    "source": "game_bundle_source",
                }
            )

    if 'purchased_feature:"buy_bonus"' in bundle and not variants_found:
        modes.append(
            {
                "id": "PURCHASE_BUY_BONUS",
                "kind": "PURCHASE",
                "request": {
                    "purchased_feature": "buy_bonus",
                    "bet_type": bet_type,
                },
                "expected_multiplier": None,
                "source": "game_bundle_source",
            }
        )

    known = {
        str(mode["request"].get("purchased_feature") or "")
        for mode in modes
        if isinstance(mode.get("request"), dict)
    }
    for feature in sorted(
        set(re.findall(r'purchased_feature:"([A-Za-z0-9_]+)"', bundle))
    ):
        if not feature or feature in known:
            continue
        modes.append(
            {
                "id": f"PURCHASE_{feature.upper()}",
                "kind": "PURCHASE",
                "request": {
                    "purchased_feature": feature,
                    "bet_type": bet_type,
                },
                "expected_multiplier": None,
                "source": "game_bundle_source",
            }
        )

    return modes

def _result_summary(data: dict[str, Any]) -> dict[str, Any]:
    result = data.get("result")
    if not isinstance(result, dict):
        result = {}
    resp = result.get("resp")
    if not isinstance(resp, dict):
        resp = {}
    common = resp.get("commonGame")
    if not isinstance(common, dict):
        common = {}
    common_data = common.get("data")
    table_hash = ""
    if isinstance(common_data, list):
        table_hash = hashlib.sha256(
            json.dumps(
                common_data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12]
    game = resp.get("game")
    if not isinstance(game, dict):
        game = {}
    total_win = resp.get("totalWin")
    if not isinstance(total_win, (int, float)):
        total_win = game.get("totalWin")
    return {
        "final": bool(result.get("final")),
        "balance": result.get("balance"),
        "state_lock": result.get("state_lock"),
        "round_step": resp.get("roundStep"),
        "bet": resp.get("bet"),
        "total_win": total_win,
        "freespins": resp.get("freespins"),
        "next_action": resp.get("nextAction"),
        "table_sha256": table_hash,
        "response_sha256": response_fingerprint(data),
    }


def run_hyperhive_test(
    *,
    game: Game,
    runtime: BGamingRuntime,
    spins: int,
    timeout_s: float,
    stop_event: threading.Event,
    progress: Progress,
    run_dir: Path,
    started_iso: str,
    started_monotonic: float,
) -> GameTestResult:
    token = str(runtime.options.get("play_token") or "").strip()
    if not token:
        raise ValueError("BGaming HyperHive: window.__OPTIONS__ sin play_token.")

    run_dir = run_dir.parent / run_dir.name.replace(
        "bgaming-http-api-v2",
        "bgaming-hyperhive-jsonrpc",
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    rpc_id = str(uuid.uuid4())
    init_response, init_request, init_data = _rpc(
        runtime,
        "init",
        timeout_s=timeout_s,
        params={"token": token, "id": ""},
        rpc_id=rpc_id,
    )
    init_result = init_data["result"]
    config = init_result.get("config")
    if not isinstance(config, dict):
        raise ValueError("BGaming HyperHive: init sin config.")

    default_bet = config.get("default_bet")
    bet_limits = config.get("bet_limits")
    if not isinstance(default_bet, (int, float)) or default_bet <= 0:
        if isinstance(bet_limits, list):
            numeric = [
                value
                for value in bet_limits
                if isinstance(value, (int, float)) and value > 0
            ]
            default_bet = min(numeric) if numeric else None
    if not isinstance(default_bet, (int, float)) or default_bet <= 0:
        raise ValueError("BGaming HyperHive: init sin apuesta utilizable.")

    current_balance = init_result.get("balance")
    if not isinstance(current_balance, (int, float)):
        raise ValueError("BGaming HyperHive: init sin balance numérico.")

    safe_init_request = _safe_json(init_request)
    safe_init_data = _safe_json(init_data)
    (run_dir / "init-request.json").write_text(
        json.dumps(safe_init_request, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "init-response.json").write_text(
        json.dumps(safe_init_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    modes = discover_modes_from_bundle(runtime, timeout_s=timeout_s)
    state_lock = init_result.get("state_lock")
    discovered_modes = [
        {
            "id": mode["id"],
            "kind": mode["kind"],
            "observed": True,
            "wire_method": "play",
            "request_options": dict(mode["request"]),
            "source": mode["source"],
        }
        for mode in modes
    ]

    progress(
        f"[{game.name}] HyperHive JSON-RPC detectado: /api, "
        f"bet={default_bet}, balance={current_balance}, modos={len(modes)}."
    )
    for mode in modes[1:]:
        progress(
            f"[{game.name}] HyperHive modo detectado: {mode['id']} "
            f"(bundle={mode['source']})."
        )

    attempts: list[SpinAttempt] = []
    errors: list[str] = []
    successes = 0
    responded = 0
    repetitions = max(1, int(spins))
    requested_total = repetitions * len(modes)

    for mode in modes:
        mode_id = str(mode["id"])
        kind = str(mode["kind"])
        for repetition in range(1, repetitions + 1):
            if stop_event.is_set():
                break
            attempt_dir = run_dir / mode_id / f"attempt-{repetition:03d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            attempt_started = time.monotonic()
            steps = 0
            warnings: list[str] = []
            last_status: int | None = None

            try:
                before_balance = float(current_balance)
                request_spec = {"bet": default_bet, **dict(mode["request"])}
                play_params: dict[str, Any] = {
                    "token": token,
                    "req": request_spec,
                }
                if state_lock:
                    play_params["state_lock"] = state_lock
                response, request_payload, data = _rpc(
                    runtime,
                    "play",
                    timeout_s=timeout_s,
                    params=play_params,
                    rpc_id=rpc_id,
                )
                responded += 1
                steps = 1
                last_status = int(response.status_code)
                first_summary = _result_summary(data)
                final_summary = first_summary
                if first_summary.get("state_lock"):
                    state_lock = first_summary["state_lock"]

                (attempt_dir / "step-001-request.json").write_text(
                    json.dumps(_safe_json(request_payload), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                (attempt_dir / "step-001-response.json").write_text(
                    json.dumps(_safe_json(data), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                first_balance = first_summary.get("balance")
                if not isinstance(first_balance, (int, float)):
                    warnings.append("HyperHive play sin balance numérico")
                    first_balance = before_balance

                guard = 256
                while not bool(final_summary.get("final")) and not stop_event.is_set():
                    if steps >= guard:
                        warnings.append(f"HyperHive guard alcanzado ({guard})")
                        break
                    base_bet_type = str(
                        dict(modes[0]["request"]).get("bet_type") or "bet"
                    )
                    next_action = str(
                        final_summary.get("next_action") or ""
                    ).strip().casefold()
                    continuation_req: dict[str, Any] = {
                        "bet": default_bet,
                        "bet_type": base_bet_type,
                    }
                    if next_action:
                        continuation_req["action"] = next_action
                    play_params = {
                        "token": token,
                        "req": continuation_req,
                    }
                    if state_lock:
                        play_params["state_lock"] = state_lock
                    response, request_payload, data = _rpc(
                        runtime,
                        "play",
                        timeout_s=timeout_s,
                        params=play_params,
                        rpc_id=rpc_id,
                    )
                    steps += 1
                    last_status = int(response.status_code)
                    final_summary = _result_summary(data)
                    if final_summary.get("state_lock"):
                        state_lock = final_summary["state_lock"]
                    (attempt_dir / f"step-{steps:03d}-request.json").write_text(
                        json.dumps(
                            _safe_json(request_payload),
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    (attempt_dir / f"step-{steps:03d}-response.json").write_text(
                        json.dumps(_safe_json(data), ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )

                final_balance = final_summary.get("balance")
                total_win = final_summary.get("total_win")
                if not isinstance(final_balance, (int, float)):
                    warnings.append("HyperHive resultado final sin balance")
                if not isinstance(total_win, (int, float)):
                    warnings.append("HyperHive resultado final sin totalWin")
                    total_win = 0

                if bool(final_summary.get("final")) and isinstance(final_balance, (int, float)):
                    if steps > 1:
                        observed_debit = before_balance - float(first_balance)
                    else:
                        observed_debit = (
                            before_balance
                            + float(total_win)
                            - float(final_balance)
                        )
                    expected_final = (
                        before_balance - observed_debit + float(total_win)
                    )
                    if abs(float(final_balance) - expected_final) > 1e-9:
                        warnings.append(
                            "HyperHive balance inconsistente: "
                            f"actual={final_balance}, esperado={expected_final}"
                        )

                    expected_multiplier = mode.get("expected_multiplier")
                    if isinstance(expected_multiplier, (int, float)):
                        expected_debit = float(default_bet) * float(expected_multiplier)
                        if abs(observed_debit - expected_debit) > 1e-9:
                            warnings.append(
                                f"HyperHive débito {observed_debit:g} != "
                                f"esperado {expected_debit:g}"
                            )
                    current_balance = final_balance
                else:
                    observed_debit = None

                terminal = bool(final_summary.get("final"))
                validated = terminal and not warnings
                successes += int(validated)
                proof = {
                    "runtime": "hyperhive-jsonrpc",
                    "mode_id": mode_id,
                    "steps": steps,
                    "before_balance": before_balance,
                    "observed_debit": observed_debit,
                    "final": terminal,
                    **final_summary,
                }
                (attempt_dir / "remote-proof.json").write_text(
                    json.dumps(proof, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                elapsed_ms = (time.monotonic() - attempt_started) * 1000.0
                attempts.append(
                    SpinAttempt(
                        number=repetition,
                        ok=True,
                        mode_id=mode_id,
                        mode_kind=kind,
                        status_code=last_status,
                        elapsed_ms=elapsed_ms,
                        symbol=game.symbol,
                        endpoint=_origin(runtime.launch_url) + "/api",
                        terminal=terminal,
                        wire_steps=steps,
                        warning="; ".join(warnings),
                        artifact_dir=str(attempt_dir),
                    )
                )
                progress(
                    f"[{game.name}] {mode_id} {repetition}/{repetitions}: "
                    f"{'OK' if validated else 'PARCIAL'} {elapsed_ms:.0f} ms, "
                    f"steps={steps}, HTTP={last_status or '—'}, "
                    f"final={terminal}, debit={observed_debit if observed_debit is not None else '—'}, "
                    f"win={total_win}, balance={current_balance}, "
                    f"resp={final_summary.get('response_sha256') or '—'}"
                    + (
                        ", diagnóstico=" + " | ".join(warnings[:3])
                        if warnings
                        else ""
                    )
                )
            except Exception as exc:
                message = sanitize_error_text(f"{type(exc).__name__}: {exc}")
                errors.append(message)
                elapsed_ms = (time.monotonic() - attempt_started) * 1000.0
                attempts.append(
                    SpinAttempt(
                        number=repetition,
                        ok=False,
                        mode_id=mode_id,
                        mode_kind=kind,
                        elapsed_ms=elapsed_ms,
                        symbol=game.symbol,
                        endpoint=_origin(runtime.launch_url) + "/api",
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

        if stop_event.is_set():
            break

    attempted = len(attempts)
    if attempted and successes == requested_total:
        status = "OK"
        error = ""
    elif responded:
        status = "PARCIAL"
        error = (
            f"BGaming HyperHive respondió {responded}/{requested_total}; "
            f"modos validados={successes}/{requested_total}."
        )
        if errors:
            error += " Errores: " + " | ".join(errors[:3])
    else:
        status = "ERROR"
        error = errors[0] if errors else "BGaming HyperHive sin respuestas válidas."

    elapsed_total = (time.monotonic() - started_monotonic) * 1000.0
    return GameTestResult(
        provider="bgaming",
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
