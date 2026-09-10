from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from tester_spin.providers.rubyplay import runtime as _runtime


# Wire shapes demonstrated by captured RubyPlay gameserver traffic. These are
# provider-family actions, not game-name routes. A purchased feature remains one
# logical test iteration while these manual continuation clicks are replayed.
PURCHASE_CONTINUATIONS = frozenset({"respin", "freespin"})

_JS_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_ORIGINAL_DISCOVER_CLIENT_PROFILE = _runtime.discover_client_profile


def _parse_integral_js_number(raw: str) -> int | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value != value.to_integral_value():
        return None
    return int(value)


def _unique_integral_js_number(values: list[str]) -> int | None:
    parsed: list[int] = []
    for raw in values:
        value = _parse_integral_js_number(raw)
        if value is not None and value not in parsed:
            parsed.append(value)
    return parsed[0] if len(parsed) == 1 else None


def discover_client_profile(
    scripts: list[tuple[str, str]],
) -> _runtime.RubyPlayClientProfile:
    """Extend RubyPlay client discovery with full JavaScript numeric literals.

    Minified engines may encode an integer math-version base using scientific
    notation (for example ``2025103e3``). The original decimal-only regex could
    silently read the prefix ``2025103`` and send the wrong v_math. We retain all
    existing discovery and only replace math_version when the client expression
    is structurally unambiguous.
    """
    profile = _ORIGINAL_DISCOVER_CLIENT_PROFILE(scripts)
    contract_parts = [
        text
        for _url, text in scripts
        if text
        and (
            "com.gongxigames.math" in text
            or "v_protocol" in text
            or "MATH_VERSION" in text
        )
    ]
    bundle = "\n".join(contract_parts)
    if not bundle:
        return profile

    bases = re.findall(
        rf"MATH_VERSION\s*=\s*({_JS_NUMBER})\s*\+\s*"
        r"[A-Za-z_$][A-Za-z0-9_$]*\.RTP",
        bundle,
        re.I,
    )
    base = _unique_integral_js_number(bases)
    rtp = profile.rtp
    if base is not None and isinstance(rtp, (int, float)) and float(rtp).is_integer():
        corrected = base + int(rtp)
        if profile.math_version != corrected:
            profile.math_version = corrected
            profile.evidence.append("client.engine.MATH_VERSION=js-number+RTP")
        return profile

    direct = re.findall(
        rf"\.MATH_VERSION\s*=\s*({_JS_NUMBER})(?![A-Za-z0-9_$\.])",
        bundle,
        re.I,
    )
    direct_value = _unique_integral_js_number(direct)
    if direct_value is not None and profile.math_version != direct_value:
        profile.math_version = direct_value
        profile.evidence.append("client.engine.MATH_VERSION=js-number")
    return profile


def post_action(
    runtime: _runtime.RubyPlayRuntime,
    action: str,
    *,
    timeout_s: float,
    bet: int | float | None = None,
    buy_feature_type: str = "",
    buy_feature_price: int | float | None = None,
):
    """Send one RubyPlay gameserver action using the observed family envelope.

    Purchased freespin/respin rounds are manual UI clicks, but on the wire they
    are continuation actions: no new bet/price is sent, while buy_feature_type is
    carried forward. Natural continuations keep the field absent unless the
    server has already exposed an active purchased feature type.
    """
    command = str(action or "").strip().lower()
    if not command or command == "init":
        raise ValueError("RubyPlay: post_action requiere una acción posterior a init.")

    previous_an = runtime.action_number
    payload: dict[str, Any] = {
        "v_protocol": runtime.client_profile.protocol_version,
        "v_math": runtime.client_profile.math_version,
        "an": previous_an,
        "bets": list(runtime.bets),
        "key": runtime.session_key,
        "device_type": "desktop",
        "funModeData": dict(runtime.fun_mode_data),
        "action": command,
    }

    if command in {"spin", "buy_feature"}:
        if not isinstance(bet, (int, float)) or isinstance(bet, bool) or bet <= 0:
            raise ValueError(f"RubyPlay {command}: bet requerido.")
        if bet not in runtime.bets:
            raise ValueError(f"RubyPlay {command}: bet no anunciado por init: {bet!r}.")
        payload["bet"] = bet

    feature_type = str(buy_feature_type or runtime.active_feature_type or "").strip().lower()
    if command == "buy_feature":
        if not feature_type:
            raise ValueError("RubyPlay buy_feature: tipo no descubierto.")
        if (
            not isinstance(buy_feature_price, (int, float))
            or isinstance(buy_feature_price, bool)
            or buy_feature_price <= 0
        ):
            raise ValueError("RubyPlay buy_feature: precio no descubierto.")
        payload["buy_feature_type"] = feature_type
        payload["buy_feature_price"] = buy_feature_price
    elif command in PURCHASE_CONTINUATIONS and feature_type:
        # Both supplied HAR families prove the same continuation envelope:
        # common state + action + buy_feature_type, with no bet or purchase price.
        payload["buy_feature_type"] = feature_type

    response = runtime.session.post(
        runtime.gameserver_url,
        json=payload,
        timeout=timeout_s,
    )
    data = _runtime._load_json_response(response, f"gameserver/{command}")
    if str(data.get("status") or "").lower() != "ok":
        raise ValueError(f"RubyPlay {command}: status={data.get('status')!r}.")

    body = data.get("data")
    if not isinstance(body, dict):
        raise ValueError(f"RubyPlay {command}: falta data.")
    try:
        new_an = int(body.get("an"))
    except (TypeError, ValueError):
        raise ValueError(f"RubyPlay {command}: data.an inválido.")
    next_action = str(body.get("next_action") or "").strip().lower()
    if not next_action:
        raise ValueError(f"RubyPlay {command}: data.next_action vacío.")

    response_fun_mode_data = data.get("funModeData")
    if isinstance(response_fun_mode_data, dict):
        runtime.fun_mode_data = dict(response_fun_mode_data)
    runtime.action_number = new_an
    runtime.next_action = next_action

    response_feature_type = str(body.get("buy_feature_type") or "").strip().lower()
    if response_feature_type:
        runtime.active_feature_type = response_feature_type
    elif command == "buy_feature" and feature_type:
        runtime.active_feature_type = feature_type
    if next_action == "spin":
        runtime.active_feature_type = ""

    return response, payload, data, previous_an


def install_runtime_contracts() -> None:
    """Install HAR-proven RubyPlay runtime contracts after adapter import."""
    from tester_spin.providers.rubyplay import execution as _execution

    _runtime.discover_client_profile = discover_client_profile
    _runtime.post_action = post_action
    _execution.post_action = post_action
    _execution.SAFE_CONTINUATIONS.update(PURCHASE_CONTINUATIONS)
