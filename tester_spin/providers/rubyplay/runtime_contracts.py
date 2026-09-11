from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from tester_spin.providers.rubyplay import runtime as _runtime


# Provider-family wire shapes demonstrated by captured RubyPlay gameserver traffic
# plus the generated client action builders. These are not routed by game name.
# A purchased/natural feature remains one logical test iteration while these
# manual continuation clicks are replayed until response.data.next_action=spin.
STATE_CONTINUATIONS = frozenset({"respin", "freespin", "minispin", "select", "pick"})
INDEX_CONTINUATIONS = frozenset({"select", "pick"})

_JS_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_JS_IDENT = r"[A-Za-z_$][A-Za-z0-9_$]*"
_ORIGINAL_DISCOVER_CLIENT_PROFILE = _runtime.discover_client_profile
_AUTO_PICK_INDEX: dict[tuple[int, str], int] = {}


def _parse_integral_js_number(raw: str) -> int | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value != value.to_integral_value():
        return None
    return int(value)


def _parse_positive_js_number(raw: str) -> float | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return float(value)


def _unique_integral_js_number(values: list[str]) -> int | None:
    parsed: list[int] = []
    for raw in values:
        value = _parse_integral_js_number(raw)
        if value is not None and value not in parsed:
            parsed.append(value)
    return parsed[0] if len(parsed) == 1 else None


def _active_slot_engine_alias(bundle: str) -> tuple[str, str, int] | None:
    """Return (engine_class, constants_alias, class_offset) for the wired engine.

    Generated RubyPlay bundles can retain multiple/old math engines. The live
    engine is structurally wired by a class instantiated by the binary/client
    factory. Its constructor references either ``ALIAS.MATH_VERSION`` or the
    generated lazy getter ``ALIAS.MATH_VERSION_$LI$()`` and getMaxWager returns
    ``ALIAS.WAGER``.
    """
    candidates: list[tuple[str, str, int]] = []
    class_re = re.compile(
        rf"var\s+({_JS_IDENT})=class\s+extends\s+{_JS_IDENT}\{{(.*?)\}};\1\.__class=",
        re.S,
    )
    for match in class_re.finditer(bundle or ""):
        engine_class = match.group(1)
        body = match.group(2)
        math_ref = re.search(
            rf"super\(\s*({_JS_IDENT})\.MATH_VERSION(?:_\$LI\$\(\))?\s*\)",
            body,
        )
        if not math_ref:
            continue
        alias = math_ref.group(1)
        wager_ref = re.search(
            rf"getMaxWager\(\)\{{return\s+({_JS_IDENT})\.WAGER\}}",
            body,
        )
        if wager_ref and wager_ref.group(1) != alias:
            continue

        instantiated = bool(
            re.search(
                rf"initBinaryFactory\(new\s+{re.escape(engine_class)}\b",
                bundle,
            )
            or re.search(
                rf"createProxy\(\)\{{return\s+new\s+{_JS_IDENT}\(new\s+{re.escape(engine_class)}\b",
                bundle,
            )
        )
        if not instantiated:
            continue
        candidate = (engine_class, alias, match.start())
        if candidate not in candidates:
            candidates.append(candidate)

    return candidates[0] if len(candidates) == 1 else None


def _nearest_alias_source(bundle: str, alias: str, before: int) -> str:
    """Resolve generated aliases such as ``I=f`` nearest the live engine.

    Minified identifiers are reused across unrelated modules, so a global alias
    map is unsafe. Only the nearest identifier assignment before the structurally
    selected engine is considered.
    """
    assignments = list(
        re.finditer(
            rf"\b{re.escape(alias)}\s*=\s*({_JS_IDENT})\b",
            (bundle or "")[: max(0, int(before))],
        )
    )
    if not assignments:
        return ""
    source = assignments[-1].group(1)
    if source in {"void", "null", "true", "false", "undefined"}:
        return ""
    return source


def _math_version_for_alias(
    bundle: str,
    alias: str,
    rtp: float | None,
) -> int | None:
    escaped = re.escape(alias)
    bases = re.findall(
        rf"\b{escaped}\.MATH_VERSION\s*=\s*({_JS_NUMBER})\s*\+\s*"
        rf"{escaped}\.RTP",
        bundle,
        re.I,
    )
    base = _unique_integral_js_number(bases)
    if (
        base is not None
        and isinstance(rtp, (int, float))
        and not isinstance(rtp, bool)
        and float(rtp).is_integer()
    ):
        return base + int(rtp)

    direct = re.findall(
        rf"\b{escaped}\.MATH_VERSION\s*=\s*({_JS_NUMBER})(?![A-Za-z0-9_$\.])",
        bundle,
        re.I,
    )
    return _unique_integral_js_number(direct)


def _alias_math_version(
    bundle: str,
    alias: str,
    rtp: float | None,
    *,
    engine_offset: int,
) -> int | None:
    value = _math_version_for_alias(bundle, alias, rtp)
    if value is not None:
        return value

    # Generated modules often export the constants class through a local alias:
    # ``var f=class{MATH_VERSION_$LI$...}; ... I=f; ... new Engine(I...)``.
    source = _nearest_alias_source(bundle, alias, engine_offset)
    if source and source != alias:
        return _math_version_for_alias(bundle, source, rtp)
    return None


def _alias_wager(bundle: str, alias: str) -> float | None:
    values = re.findall(
        rf"\b{re.escape(alias)}\.WAGER\s*=\s*({_JS_NUMBER})(?![A-Za-z0-9_$\.])",
        bundle,
        re.I,
    )
    parsed: list[float] = []
    for raw in values:
        value = _parse_positive_js_number(raw)
        if value is not None and value not in parsed:
            parsed.append(value)
    return parsed[0] if len(parsed) == 1 else None


def discover_client_profile(
    scripts: list[tuple[str, str]],
) -> _runtime.RubyPlayClientProfile:
    """Discover the client contract without global numeric-value guessing."""
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

    active = _active_slot_engine_alias(bundle)
    if active is not None:
        engine_class, alias, engine_offset = active
        active_math = _alias_math_version(
            bundle,
            alias,
            profile.rtp,
            engine_offset=engine_offset,
        )
        active_wager = _alias_wager(bundle, alias)
        if active_math is not None:
            profile.math_version = active_math
            profile.evidence.append(
                f"client.active-slot-engine.{engine_class}->{alias}.MATH_VERSION"
            )
        if active_wager is not None:
            profile.wager = active_wager
            profile.evidence.append(
                f"client.active-slot-engine.{engine_class}->{alias}.WAGER"
            )
        if active_math is not None or active_wager is not None:
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


def _pick_cursor_key(runtime: _runtime.RubyPlayRuntime) -> tuple[int, str]:
    return (id(runtime), str(runtime.session_key or ""))


def post_action(
    runtime: _runtime.RubyPlayRuntime,
    action: str,
    *,
    timeout_s: float,
    bet: int | float | None = None,
    buy_feature_type: str = "",
    buy_feature_price: int | float | None = None,
    action_index: int | None = None,
):
    """Send one RubyPlay gameserver action using the observed family envelope.

    ``select`` and ``pick`` use the provider client's INDEX field. The executor
    can therefore keep following ``next_action`` without game-specific branches:
    select deterministically chooses the first valid option (index 0), while a
    consecutive pick chain uses distinct indices 0,1,2,... because the generated
    PickMessageHandler rejects duplicate picks. ``minispin``, ``freespin`` and
    ``respin`` have no action-specific numeric argument.
    """
    command = str(action or "").strip().lower()
    if not command or command == "init":
        raise ValueError("RubyPlay: post_action requiere una acción posterior a init.")

    pick_key = _pick_cursor_key(runtime)
    if command in {"spin", "buy_feature", "select"}:
        _AUTO_PICK_INDEX.pop(pick_key, None)

    if command == "select" and action_index is None:
        action_index = 0
    elif command == "pick" and action_index is None:
        action_index = _AUTO_PICK_INDEX.get(pick_key, 0)
        _AUTO_PICK_INDEX[pick_key] = action_index + 1

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

    if command in INDEX_CONTINUATIONS:
        if not isinstance(action_index, int) or isinstance(action_index, bool) or action_index < 0:
            raise ValueError(f"RubyPlay {command}: index entero >= 0 requerido.")
        payload["index"] = action_index

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
    elif command in STATE_CONTINUATIONS and feature_type:
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

    if command == "pick" and next_action != "pick":
        _AUTO_PICK_INDEX.pop(pick_key, None)
    if next_action == "spin":
        _AUTO_PICK_INDEX.pop(pick_key, None)
        runtime.active_feature_type = ""

    return response, payload, data, previous_an


def install_runtime_contracts() -> None:
    """Install HAR/client-proven RubyPlay runtime contracts after adapter import."""
    from tester_spin.providers.rubyplay import execution as _execution

    _runtime.discover_client_profile = discover_client_profile
    _runtime.post_action = post_action
    _execution.post_action = post_action
    _execution.SAFE_CONTINUATIONS.update(STATE_CONTINUATIONS)
