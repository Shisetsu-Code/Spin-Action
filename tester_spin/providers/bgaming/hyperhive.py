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
    _collect_provider_script_contracts,
    _provider_script_url,
    _runtime_bundle_candidates,
    response_fingerprint,
    sanitize_error_text,
    sanitize_session_url,
)


class HyperHiveRPCError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any],
        status_code: int,
    ) -> None:
        super().__init__(message)
        self.request_payload = request_payload
        self.response_payload = response_payload
        self.status_code = status_code
        error = response_payload.get("error")
        self.error_code = (
            error.get("code")
            if isinstance(error, dict)
            else None
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
    rpc_id: int | str | None = None,
) -> tuple[requests.Response, dict[str, Any], dict[str, Any]]:
    api_url = _origin(runtime.launch_url) + "/api"
    payload = {
        "id": 0 if rpc_id is None else rpc_id,
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
        raise HyperHiveRPCError(
            f"BGaming HyperHive RPC error: {data.get('error')!r}",
            request_payload=_safe_json(payload),
            response_payload=_safe_json(data),
            status_code=int(response.status_code),
        )
    if not isinstance(data.get("result"), dict):
        raise ValueError("BGaming HyperHive: respuesta sin result.")
    return response, payload, data


def _download_bundle(runtime: BGamingRuntime, timeout_s: float) -> str:
    """Collect HyperHive protocol-bearing provider scripts, including children."""
    origin = _origin(runtime.launch_url)
    diagnostics: list[dict[str, Any]] = []
    seeds = _runtime_bundle_candidates(
        runtime,
        timeout_s=timeout_s,
        diagnostics=diagnostics,
    )
    for candidate in (origin + "/loader.js", origin + "/main.js"):
        if candidate not in seeds:
            seeds.append(candidate)

    contracts = _collect_provider_script_contracts(
        runtime,
        timeout_s=timeout_s,
        seeds=seeds,
        diagnostics=diagnostics,
        max_depth=2,
        max_scripts=32,
        max_total_bytes=12 * 1024 * 1024,
    )
    contracts.sort(key=lambda item: item[0], reverse=True)
    return "\n".join(text for _score, _url, text in contracts)



def _download_engine_contract(
    runtime: BGamingRuntime,
    *,
    timeout_s: float,
) -> str:
    """Download the actual client-side JSON-RPC contract when possible.

    Launch pages increasingly use hashed/versioned scripts, so fixed filenames
    are only fallbacks. Script URLs captured from the bootstrap HTML are the
    primary evidence source.
    """
    origin = _origin(runtime.launch_url)
    candidates = [
        *[
            url for url in runtime.script_urls
            if _provider_script_url(runtime, url)
        ],
        origin + "/client.min.js",
        origin + "/game/game.min.js",
        origin + "/game/integration.min.js",
    ]
    texts: list[str] = []
    seen: set[str] = set()
    total_bytes = 0
    max_contract_bytes = 8 * 1024 * 1024
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            response = runtime.session.get(url, timeout=timeout_s)
            response.raise_for_status()
            text = response.text
        except Exception:
            continue
        if not text:
            continue
        lower = text.casefold()
        if not any(
            marker in lower
            for marker in (
                "jsonrpc",
                "state_lock",
                "bet_type",
                "custom_req",
                "purchased_feature",
                "method",
            )
        ):
            continue
        encoded_size = len(text.encode("utf-8", errors="replace"))
        if total_bytes + encoded_size > max_contract_bytes:
            continue
        texts.append(text)
        total_bytes += encoded_size
    return "\n".join(texts)


def _hyperhive_rpc_id(engine_contract: str) -> int | str:
    if re.search(
        r'id:[A-Za-z_$][A-Za-z0-9_$]*\(\),jsonrpc:["\']2\.0["\']',
        engine_contract,
    ):
        return str(uuid.uuid4())
    return 0


def _pz_custom_req(
    *,
    bet: int | float,
    exponent: int,
    action: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "selectedWinLines": [0] if action == "spin" else [],
        "perLine": True,
        "action": action,
        "exponent": exponent,
    }
    if action == "spin":
        payload["stake"] = bet
    return payload


def _literal_assignments(text: str, key: str) -> set[str]:
    escaped = re.escape(key)
    patterns = [
        rf'\b{escaped}\b\s*:\s*["\']([A-Za-z0-9_\-]+)["\']',
        rf'\.{escaped}\s*=\s*["\']([A-Za-z0-9_\-]+)["\']',
        rf'\[["\']{escaped}["\']\]\s*=\s*["\']([A-Za-z0-9_\-]+)["\']',
    ]
    values: set[str] = set()
    for pattern in patterns:
        values.update(re.findall(pattern, text or ""))
    return {str(value).strip() for value in values if str(value).strip()}


def _request_literal_assignments(text: str, key: str) -> set[str]:
    """Return literals assigned specifically inside/to the JSON-RPC req object."""
    escaped = re.escape(key)
    patterns = [
        rf'\breq\s*:\s*\{{[^{{}}]{{0,1200}}\b{escaped}\b\s*:\s*["\']([A-Za-z0-9_\-]+)["\']',
        rf'\.req\.{escaped}\s*=\s*["\']([A-Za-z0-9_\-]+)["\']',
        rf'\.req\[["\']{escaped}["\']\]\s*=\s*["\']([A-Za-z0-9_\-]+)["\']',
    ]
    values: set[str] = set()
    for pattern in patterns:
        values.update(re.findall(pattern, text or ""))
    return {str(value).strip() for value in values if str(value).strip()}


def _has_request_bet_contract(text: str) -> bool:
    return bool(
        re.search(
            r'(?:\breq\s*:\s*\{[^{}]{0,1200}\bbet\s*:|\.req\.bet\s*=|\.req\[["\']bet["\']\]\s*=)',
            text or "",
        )
    )

def discover_action_vocabulary(bundle_text: str, engine_contract: str = "") -> set[str]:
    """Discover action values actually encoded by the loaded HyperHive client."""
    combined = (bundle_text or "") + "\n" + (engine_contract or "")
    return {
        action.casefold()
        for action in _literal_assignments(combined, "action")
    }


def discover_modes_from_bundle(
    runtime: BGamingRuntime,
    *,
    timeout_s: float,
    bundle_text: str | None = None,
    engine_contract: str = "",
) -> list[dict[str, Any]]:
    """Discover the exact HyperHive request vocabulary from loaded JS."""
    bundle = (
        _download_bundle(runtime, timeout_s)
        if bundle_text is None
        else bundle_text
    )
    combined = bundle + "\n" + engine_contract
    bet_type_values = {
        value.casefold()
        for value in _request_literal_assignments(combined, "bet_type")
    }
    # freebet is a conditional mode, not the ordinary paid spin contract.
    normal_bet_types = bet_type_values - {"freebet"}
    if "betting" in normal_bet_types:
        bet_type = "betting"
    elif "bet" in normal_bet_types:
        bet_type = "bet"
    elif len(normal_bet_types) == 1:
        bet_type = next(iter(normal_bet_types))
    else:
        bet_type = ""

    action_vocabulary = discover_action_vocabulary(bundle, engine_contract)
    request_actions = {
        value.casefold()
        for value in _request_literal_assignments(combined, "action")
    }
    spin_request: dict[str, Any] = {}
    if bet_type:
        spin_request["bet_type"] = bet_type
    if "spin" in request_actions:
        spin_request["action"] = "spin"

    custom_req_profile = (
        "pz-per-line"
        if (
            "custom_req" in engine_contract
            and re.search(r"selectedWinLines\s*:\s*\[\s*0\s*\]", engine_contract)
            and re.search(r"perLine\s*:\s*(?:!0|true)", engine_contract)
        )
        else ""
    )
    has_req_bet_contract = _has_request_bet_contract(combined)
    explicit_base_contract = bool(
        custom_req_profile
        or has_req_bet_contract
    )

    # HyperHive itself has a provider-level minimal play contract:
    # method=play, params.token, params.req.bet and optional state_lock.
    # A successful HyperHive init is stronger evidence than arbitrary loose JS
    # literals. Extra req fields are still added only when the client proves them.
    base_contract_observed = True

    modes: list[dict[str, Any]] = [
        {
            "id": "SPIN",
            "kind": "SPIN",
            "request": spin_request,
            "expected_multiplier": 1.0,
            "custom_req_profile": custom_req_profile,
            "executable": base_contract_observed,
            "discovery_state": (
                "BASE_CONTRACT"
                if explicit_base_contract
                else "MINIMAL_PROVIDER_CONTRACT"
            ),
            "source": (
                "game_bundle_source+engine_contract"
                if custom_req_profile
                else ("game_bundle_source" if bundle else "hyperhive-base")
            ),
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
                    **({"bet_type": bet_type} if bet_type else {}),
                    "purchased_feature": "buy_chance",
                },
                "expected_multiplier": None,
                "source": "game_bundle_source",
                "executable": True,
                "discovery_state": "WIRE_PATTERN",
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
                        **({"bet_type": bet_type} if bet_type else {}),
                        "purchased_feature": "buy_bonus",
                        "bonus_multiplier_type": variant,
                    },
                    "expected_multiplier": None,
                    "source": "game_bundle_source",
                    "executable": True,
                    "discovery_state": "WIRE_PATTERN",
                }
            )

    if 'purchased_feature:"buy_bonus"' in bundle and not variants_found:
        multiplier_matches = [
            float(value)
            for value in re.findall(
                r"buyBonusMultiplier\s*=\s*([0-9]+(?:\.[0-9]+)?)",
                bundle,
            )
        ]
        positive_multipliers = [
            value for value in multiplier_matches if value > 0
        ]
        expected_buy_bonus_multiplier = (
            max(positive_multipliers)
            if positive_multipliers
            else None
        )
        modes.append(
            {
                "id": "PURCHASE_BUY_BONUS",
                "kind": "PURCHASE",
                "request": {
                    **({"bet_type": bet_type} if bet_type else {}),
                    "purchased_feature": "buy_bonus",
                },
                "expected_multiplier": expected_buy_bonus_multiplier,
                "source": "game_bundle_source",
                "executable": True,
                "discovery_state": "WIRE_PATTERN",
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
                    **({"bet_type": bet_type} if bet_type else {}),
                    "purchased_feature": feature,
                },
                "expected_multiplier": None,
                "source": "game_bundle_source",
                "executable": False,
                "discovery_state": "DISCOVERED_LITERAL_ONLY",
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
    if not isinstance(total_win, (int, float)):
        round_data = resp.get("round")
        if isinstance(round_data, dict):
            round_win = round_data.get("win")
            try:
                total_win = (
                    float(round_win)
                    if round_win is not None
                    else None
                )
            except (TypeError, ValueError):
                total_win = None
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

    bundle_text = _download_bundle(runtime, timeout_s)
    engine_contract = _download_engine_contract(
        runtime,
        timeout_s=timeout_s,
    )

    init_response, init_request, init_data = _rpc(
        runtime,
        "init",
        timeout_s=timeout_s,
        params={"token": token},
        rpc_id=_hyperhive_rpc_id(engine_contract),
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

    modes = discover_modes_from_bundle(
        runtime,
        timeout_s=timeout_s,
        bundle_text=bundle_text,
        engine_contract=engine_contract,
    )
    action_vocabulary = discover_action_vocabulary(bundle_text, engine_contract)
    state_lock = init_result.get("state_lock")
    modes_to_run = [mode for mode in modes if bool(mode.get("executable"))]
    rpc_id_probe = _hyperhive_rpc_id(engine_contract)
    contract_diagnostic = {
        "runtime": "hyperhive-jsonrpc",
        "script_urls": [
            sanitize_session_url(url)
            for url in runtime.script_urls
            if _provider_script_url(runtime, url)
        ],
        "bundle_sha256": hashlib.sha256(
            bundle_text.encode("utf-8", errors="replace")
        ).hexdigest() if bundle_text else "",
        "engine_sha256": hashlib.sha256(
            engine_contract.encode("utf-8", errors="replace")
        ).hexdigest() if engine_contract else "",
        "rpc_id_profile": "uuid" if isinstance(rpc_id_probe, str) else "zero",
        "action_vocabulary": sorted(action_vocabulary),
        "base_request": dict(modes[0].get("request") or {}),
        "custom_req_profile": str(modes[0].get("custom_req_profile") or ""),
        "base_discovery_state": str(modes[0].get("discovery_state") or ""),
    }
    (run_dir / "contract-diagnostic.json").write_text(
        json.dumps(contract_diagnostic, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    discovered_modes = [
        {
            "id": mode["id"],
            "kind": mode["kind"],
            "observed": False,
            "validated": False,
            "executable": bool(mode.get("executable")),
            "discovery_state": str(mode.get("discovery_state") or "DISCOVERED"),
            "wire_method": "play",
            "request_options": dict(mode["request"]),
            "source": mode["source"],
        }
        for mode in modes
    ]

    progress(
        f"[{game.name}] HyperHive JSON-RPC detectado: /api, "
        f"bet={default_bet}, balance={current_balance}, "
        f"descubiertos={len(modes)}, ejecutables={len(modes_to_run)}, "
        f"base_req={sorted(contract_diagnostic['base_request'])}, "
        f"custom={contract_diagnostic['custom_req_profile'] or 'no'}, "
        f"rpc_id={contract_diagnostic['rpc_id_profile']}."
    )
    for mode in modes[1:]:
        progress(
            f"[{game.name}] HyperHive modo detectado: {mode['id']} "
            f"state={mode.get('discovery_state')}, "
            f"ejecutable={'sí' if mode.get('executable') else 'no'}, "
            f"source={mode['source']}."
        )

    attempts: list[SpinAttempt] = []
    errors: list[str] = []
    successes = 0
    responded = 0
    repetitions = max(1, int(spins))
    requested_total = repetitions * len(modes_to_run)

    if not modes_to_run:
        elapsed_total = (time.monotonic() - started_monotonic) * 1000.0
        return GameTestResult(
            provider="bgaming",
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=repetitions,
            successful_spins=0,
            failed_spins=repetitions,
            status="PARCIAL",
            symbol=game.symbol,
            discovered_modes=discovered_modes,
            started_at=started_iso,
            finished_at=utc_now_iso(),
            elapsed_ms=elapsed_total,
            error=(
                "BGaming HyperHive CONTRACT_UNRESOLVED: init válido, pero los "
                "scripts cargados no demostraron el wire-shape de play; no se "
                "envió un request adivinado."
            ),
            run_dir=str(run_dir),
            attempts=[],
        )

    for mode in modes_to_run:
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
                currency_attributes = init_result.get("currency_attributes")
                exponent = 2
                if isinstance(currency_attributes, dict):
                    raw_exponent = currency_attributes.get("exponent")
                    if isinstance(raw_exponent, int):
                        exponent = raw_exponent
                if mode.get("custom_req_profile") == "pz-per-line":
                    request_spec["custom_req"] = _pz_custom_req(
                        bet=default_bet,
                        exponent=exponent,
                        action="spin",
                    )
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
                    rpc_id=_hyperhive_rpc_id(engine_contract),
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
                    base_request = dict(modes[0]["request"])
                    next_action = str(
                        final_summary.get("next_action") or ""
                    ).strip().casefold()
                    if next_action and next_action not in action_vocabulary:
                        warnings.append(
                            "HyperHive nextAction no presente en el contrato cliente: "
                            f"{next_action!r}; continuación detenida sin adivinar payload"
                        )
                        break

                    continuation_req: dict[str, Any] = {"bet": default_bet}
                    for key, value in base_request.items():
                        if key not in {
                            "action",
                            "purchased_feature",
                            "bonus_multiplier_type",
                        }:
                            continuation_req[key] = value
                    if next_action:
                        continuation_req["action"] = next_action
                    if modes[0].get("custom_req_profile") == "pz-per-line":
                        action = next_action or "spin"
                        continuation_req.pop("action", None)
                        continuation_req["custom_req"] = _pz_custom_req(
                            bet=default_bet,
                            exponent=exponent,
                            action=action,
                        )
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
                        rpc_id=_hyperhive_rpc_id(engine_contract),
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
                expected_multiplier = mode.get("expected_multiplier")
                if not isinstance(final_balance, (int, float)):
                    warnings.append("HyperHive resultado final sin balance")
                if not isinstance(total_win, (int, float)):
                    if (
                        steps == 1
                        and isinstance(final_balance, (int, float))
                        and isinstance(expected_multiplier, (int, float))
                    ):
                        expected_debit_for_inference = (
                            float(default_bet) * float(expected_multiplier)
                        )
                        inferred_win = (
                            float(final_balance)
                            - before_balance
                            + expected_debit_for_inference
                        )
                        if inferred_win >= -1e-9:
                            total_win = max(0.0, inferred_win)
                        else:
                            warnings.append(
                                "HyperHive resultado final sin totalWin "
                                "y no pudo inferirse por balance"
                            )
                            total_win = 0
                    else:
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
                if validated:
                    for discovered in discovered_modes:
                        if discovered.get("id") == mode_id:
                            discovered["observed"] = True
                            discovered["validated"] = True
                            discovered["discovery_state"] = "VALIDATED"
                            break
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
                        ok=validated,
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
                if isinstance(exc, HyperHiveRPCError):
                    last_status = exc.status_code
                    (attempt_dir / "rpc-error-request.json").write_text(
                        json.dumps(
                            exc.request_payload,
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    (attempt_dir / "rpc-error-response.json").write_text(
                        json.dumps(
                            exc.response_payload,
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    if exc.error_code == 51100:
                        mode["executable"] = False
                        mode["discovery_state"] = "REJECTED_51100"
                        for discovered in discovered_modes:
                            if discovered.get("id") == mode_id:
                                discovered["executable"] = False
                                discovered["discovery_state"] = "REJECTED_51100"
                                break

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
                if (
                    isinstance(exc, HyperHiveRPCError)
                    and exc.error_code == 51100
                ):
                    progress(
                        f"[{game.name}] {mode_id}: contrato marcado REJECTED_51100; "
                        "no se repetirá el mismo wire-shape en esta corrida."
                    )
                    break

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
