from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import requests

from tester_spin.providers.redtiger.result_tree import observed_modes, result_nodes


@dataclass(frozen=True, slots=True)
class FeatureBuy:
    name: str
    multiplier: Decimal


@dataclass(slots=True)
class RedTigerRuntime:
    session: requests.Session
    settings_url: str
    spin_url: str
    launcher_url: str
    game_id: str
    session_id: str
    token: str
    user_data: dict[str, Any]
    custom: dict[str, Any]
    settings_request: dict[str, Any]
    settings_response: dict[str, Any]
    stakes: tuple[Decimal, ...]
    default_stake: Decimal
    currency_decimals: int
    feature_buys: tuple[FeatureBuy, ...]


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def settings_result(settings_response: dict[str, Any]) -> dict[str, Any]:
    if settings_response.get("success") is not True:
        raise ValueError("Red Tiger settings no devolvió success=true.")
    result = settings_response.get("result")
    if not isinstance(result, dict):
        raise ValueError("Red Tiger settings no contiene result objeto.")
    if not isinstance(result.get("game"), dict):
        raise ValueError("Red Tiger settings no contiene result.game objeto.")
    if not isinstance(result.get("user"), dict):
        raise ValueError("Red Tiger settings no contiene result.user objeto.")
    return result


def stakes_from_settings(settings_response: dict[str, Any]) -> tuple[tuple[Decimal, ...], Decimal]:
    result = settings_result(settings_response)
    user = result["user"]
    stake_info = user.get("stakes")
    if not isinstance(stake_info, dict):
        raise ValueError("Red Tiger settings no anunció user.stakes.")

    raw_types = stake_info.get("types")
    if not isinstance(raw_types, list):
        raise ValueError("Red Tiger settings user.stakes.types no es lista.")

    stakes: list[Decimal] = []
    for raw in raw_types:
        value = _decimal(raw)
        if value is not None and value > 0 and value not in stakes:
            stakes.append(value)
    if not stakes:
        raise ValueError("Red Tiger settings no anunció stakes positivos.")

    default_index = stake_info.get("defaultIndex")
    if not isinstance(default_index, int) or isinstance(default_index, bool):
        default_index = stake_info.get("lastIndex")
    if not isinstance(default_index, int) or isinstance(default_index, bool):
        default_index = 0
    if default_index < 0 or default_index >= len(stakes):
        default_index = 0
    return tuple(stakes), stakes[default_index]


def feature_buys_from_settings(settings_response: dict[str, Any]) -> tuple[FeatureBuy, ...]:
    game = settings_result(settings_response)["game"]
    raw = game.get("featureBuy")
    if not isinstance(raw, list):
        return ()

    found: list[FeatureBuy] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        multiplier = _decimal(item.get("price"))
        if not name or multiplier is None or multiplier <= 0:
            continue
        candidate = FeatureBuy(name=name, multiplier=multiplier)
        if candidate not in found:
            found.append(candidate)
    return tuple(found)


def decimal_json_number(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    if value == integral:
        return int(integral)
    return float(value)


def money_string(value: Decimal, decimals: int) -> str:
    places = max(0, min(8, int(decimals)))
    quantum = Decimal(1).scaleb(-places)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    return f"{rounded:.{places}f}"


def build_spin_payload(
    runtime: RedTigerRuntime,
    *,
    stake: Decimal,
    feature_buy: FeatureBuy | None = None,
    game_mode: int | str = 0,
) -> dict[str, Any]:
    if stake not in runtime.stakes:
        raise ValueError(f"Red Tiger stake no anunciado por settings: {stake!s}.")

    payload = {
        "token": runtime.token,
        "sessionId": runtime.session_id,
        "playMode": str(runtime.settings_request.get("playMode") or "demo"),
        "gameId": runtime.game_id,
        "userData": copy.deepcopy(runtime.user_data),
        "custom": copy.deepcopy(runtime.custom),
        "stake": decimal_json_number(stake),
        "listenToFrontend": bool(runtime.settings_request.get("listenToFrontend", True)),
        "bonusId": None,
        "extras": None,
        "gameMode": game_mode,
    }
    if feature_buy is not None:
        cost = stake * feature_buy.multiplier
        payload["extras"] = {
            "features": {
                "featureBuy": feature_buy.name,
                "featureBuyCost": money_string(cost, runtime.currency_decimals),
            }
        }
    return payload


def validate_spin_response(payload: Any) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    if not isinstance(payload, dict):
        return False, ["respuesta no es objeto JSON"]
    if payload.get("success") is not True:
        return False, [f"success={payload.get('success')!r}"]
    result = payload.get("result")
    if not isinstance(result, dict):
        return False, ["falta result objeto"]
    game = result.get("game")
    if not isinstance(game, dict):
        return False, ["falta result.game objeto"]
    if not result_nodes(game):
        warnings.append("resultado sin nodos spinMode observables")
    return True, warnings


def apply_response_token(runtime: RedTigerRuntime, payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    result = payload.get("result")
    if not isinstance(result, dict):
        return
    user = result.get("user")
    if not isinstance(user, dict):
        return
    token = str(user.get("token") or "").strip()
    session_id = str(user.get("sessionId") or "").strip()
    if token:
        runtime.token = token
    if session_id:
        runtime.session_id = session_id


def response_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"success": False, "spin_modes": [], "nodes": []}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    game = result.get("game") if isinstance(result.get("game"), dict) else {}
    nodes = result_nodes(game)
    return {
        "success": payload.get("success") is True,
        "spin_modes": observed_modes(game),
        "nodes": [
            {
                "path": node.path,
                "spin_mode": node.spin_mode,
                "game_mode": node.game_mode,
                "has_state": node.has_state,
                "feature_count": node.feature_count,
            }
            for node in nodes
        ],
    }


def sanitize_payload(value: Any) -> Any:
    sensitive = {
        "token",
        "sessionid",
        "ne_evo_token",
        "wrapper_token",
        "fingerprint",
        "cookie",
        "authorization",
    }
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, child in value.items():
            if str(key).lower() in sensitive:
                clean[str(key)] = "<redacted>" if child not in {None, ""} else child
            else:
                clean[str(key)] = sanitize_payload(child)
        return clean
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    return value
