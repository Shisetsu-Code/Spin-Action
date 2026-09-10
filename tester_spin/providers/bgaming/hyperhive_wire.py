from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any


_SAFE_LITERAL_KEY = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_SENSITIVE_LITERAL_PARTS = (
    "token",
    "secret",
    "password",
    "session",
    "csrf",
    "nonce",
    "seed",
)


@dataclass(slots=True)
class ObservedHyperHiveWire:
    """Wire facts discovered from the currently loaded HyperHive client code."""

    bet_type: str = ""
    custom_req: bool = False
    custom_action: bool = False
    custom_exponent: bool = False
    custom_stake_on_spin: bool = False
    custom_literals: dict[str, Any] = field(default_factory=dict)
    exponent: int = 2

    @property
    def custom_profile(self) -> str:
        if not self.custom_req:
            return ""
        return (
            "observed-formatted-stake"
            if self.custom_stake_on_spin
            else "observed-formatted"
        )


def _safe_literal_key(key: str) -> bool:
    text = str(key or "")
    lowered = text.casefold()
    return bool(
        _SAFE_LITERAL_KEY.fullmatch(text)
        and not any(part in lowered for part in _SENSITIVE_LITERAL_PARTS)
    )


def _parse_js_scalar(raw: str) -> Any:
    text = str(raw or "").strip()
    if text == "true":
        return True
    if text == "false":
        return False
    if text == "null":
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        value = text[1:-1]
        if len(value) <= 200:
            return value
        raise ValueError("oversized literal")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", text):
        return float(text)
    raise ValueError("dynamic expression")


def _formatted_request_literals(compact: str) -> dict[str, Any]:
    """Extract scalar defaults proven by the live client's formatted request."""
    found: dict[str, Any] = {}
    scalar = r'(?:true|false|null|-?\d+(?:\.\d+)?|"[^"\\]{0,200}"|\'[^\'\\]{0,200}\')'

    for match in re.finditer(
        rf"formattedRequest\.params\.([A-Za-z_$][A-Za-z0-9_$]*)=({scalar})",
        compact,
    ):
        key = match.group(1)
        if not _safe_literal_key(key):
            continue
        try:
            found[key] = _parse_js_scalar(match.group(2))
        except ValueError:
            continue

    for pattern in (
        r"formattedRequest\.params=\{([^{}]{1,1600})\}",
        r"formattedRequest=\{params:\{([^{}]{1,1600})\}",
    ):
        for object_match in re.finditer(pattern, compact):
            body = object_match.group(1)
            for pair in re.finditer(
                rf"(?:^|,)([A-Za-z_$][A-Za-z0-9_$]*):({scalar})(?=,|$)",
                body,
            ):
                key = pair.group(1)
                if not _safe_literal_key(key):
                    continue
                try:
                    found[key] = _parse_js_scalar(pair.group(2))
                except ValueError:
                    continue

    for dynamic in ("action", "exponent", "stake"):
        found.pop(dynamic, None)
    return found


def analyze_engine_wire(engine_contract: str) -> ObservedHyperHiveWire:
    """Extract wire facts only from scripts loaded by the current live runtime."""
    compact = re.sub(r"\s+", "", engine_contract or "")

    model_maps_bet_type = bool(
        re.search(r"bet_type:(?:this\.)?betType\b", compact)
    )
    default_bet_type = bool(
        model_maps_bet_type
        and re.search(r"\.betType=[\"']default[\"']", compact)
    )

    custom_req = bool(
        re.search(
            r"\.req\.custom_req=[^;]{0,240}formattedRequest\.params",
            compact,
        )
    )
    custom_action = bool(
        custom_req
        and re.search(r"formattedRequest\.params\.action", compact)
    )
    custom_exponent = bool(
        custom_req
        and re.search(r"formattedRequest\.params\.exponent", compact)
    )
    custom_stake_on_spin = bool(
        custom_req
        and re.search(r"formattedRequest\.params\.stake", compact)
    )

    return ObservedHyperHiveWire(
        bet_type="default" if default_bet_type else "",
        custom_req=custom_req,
        custom_action=custom_action,
        custom_exponent=custom_exponent,
        custom_stake_on_spin=custom_stake_on_spin,
        custom_literals=(
            _formatted_request_literals(compact)
            if custom_req
            else {}
        ),
    )


def apply_observed_play_wire(
    params: dict[str, Any],
    profile: ObservedHyperHiveWire,
) -> dict[str, Any]:
    """Adapt one live request using only facts discovered from the live client."""
    out = dict(params)
    raw_req = out.get("req")
    if not isinstance(raw_req, dict):
        return out
    req = dict(raw_req)

    if profile.bet_type:
        req["bet_type"] = profile.bet_type

    if profile.custom_req and "custom_req" not in req:
        action = str(req.pop("action", "") or "spin")
        custom: dict[str, Any] = dict(profile.custom_literals)
        if profile.custom_action:
            custom["action"] = action
        if profile.custom_exponent:
            custom["exponent"] = int(profile.exponent)
        if (
            profile.custom_stake_on_spin
            and action.casefold() == "spin"
            and isinstance(req.get("bet"), (int, float))
        ):
            custom["stake"] = req["bet"]
        if custom:
            req["custom_req"] = custom

    out["req"] = req
    return out


def _has_req_bet_evidence(text: str) -> bool:
    """Require direct live-client evidence that bet belongs to JSON-RPC req."""
    return bool(
        re.search(
            r'(?:\breq\s*:\s*\{[^{}]{0,1600}\bbet\s*:|\.req\.bet\s*=|\.req\[["\']bet["\']\]\s*=)',
            text or "",
        )
    )


def _has_req_bet_type_evidence(text: str) -> bool:
    """Require req-scoped bet_type evidence rather than a loose string literal."""
    return bool(
        re.search(
            r'(?:\breq\s*:\s*\{[^{}]{0,1600}\bbet_type\s*:|\.req\.bet_type\s*=|\.req\[["\']bet_type["\']\]\s*=)',
            text or "",
        )
    )


_install_lock = threading.Lock()
_installed = False
_profiles: dict[int, ObservedHyperHiveWire] = {}


def _engine_result_summary(
    data: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Read the nested HyperHive engine response shape from the live response."""
    result = data.get("result")
    resp = result.get("resp") if isinstance(result, dict) else None
    engine = resp.get("engine") if isinstance(resp, dict) else None
    gamestate = engine.get("gamestate") if isinstance(engine, dict) else None
    if not isinstance(gamestate, dict):
        return summary

    if not isinstance(summary.get("total_win"), (int, float)):
        total_winnings = gamestate.get("totalWinnings")
        if isinstance(total_winnings, (int, float)):
            summary["total_win"] = total_winnings

    if not isinstance(summary.get("bet"), (int, float)):
        stake = gamestate.get("stake")
        if isinstance(stake, (int, float)):
            summary["bet"] = stake

    if not str(summary.get("next_action") or "").strip():
        next_action = gamestate.get("nextAction")
        triggering = gamestate.get("triggeringDetails")
        if not next_action and isinstance(triggering, dict):
            next_action = triggering.get("nextAction")
        if isinstance(next_action, str) and next_action.strip():
            summary["next_action"] = next_action.strip()

    return summary


# Backward-compatible test helper name. This function parses a live response;
# it does not read, require, or apply any HAR at runtime.
_har_result_summary = _engine_result_summary


def install_observed_wire_adapter() -> None:
    """Install live-client HyperHive discovery/adaptation. HAR is not consulted."""
    global _installed
    with _install_lock:
        if _installed:
            return

        from tester_spin.providers.bgaming import hyperhive

        original_download_engine_contract = hyperhive._download_engine_contract
        original_discover_modes = hyperhive.discover_modes_from_bundle
        original_rpc = hyperhive._rpc
        original_result_summary = hyperhive._result_summary

        def observed_download_engine_contract(
            runtime,
            *,
            timeout_s: float,
        ) -> str:
            text = original_download_engine_contract(runtime, timeout_s=timeout_s)
            _profiles[id(runtime)] = analyze_engine_wire(text)
            return text

        def observed_discover_modes(
            runtime,
            *,
            timeout_s: float,
            bundle_text: str | None = None,
            engine_contract: str = "",
        ):
            resolved_bundle = (
                hyperhive._download_bundle(runtime, timeout_s)
                if bundle_text is None
                else bundle_text
            )
            modes = original_discover_modes(
                runtime,
                timeout_s=timeout_s,
                bundle_text=resolved_bundle,
                engine_contract=engine_contract,
            )
            profile = analyze_engine_wire(engine_contract)
            _profiles[id(runtime)] = profile
            combined = resolved_bundle + "\n" + (engine_contract or "")
            req_bet_observed = _has_req_bet_evidence(combined)
            req_bet_type_observed = _has_req_bet_type_evidence(combined)

            if modes:
                base = modes[0]
                request = base.get("request")
                if isinstance(request, dict):
                    if (
                        request.get("bet_type") == "bet"
                        and not req_bet_type_observed
                        and not profile.bet_type
                    ):
                        request.pop("bet_type", None)

                # Fail closed from live evidence only. Historical HARs are useful
                # to design/test this parser, never to authorize a live wager.
                if not req_bet_observed:
                    base["executable"] = False
                    base["discovery_state"] = "CONTRACT_UNRESOLVED"
                    base["source"] = "live-client-evidence-incomplete"
                    for mode in modes[1:]:
                        if mode.get("kind") == "PURCHASE" and mode.get("executable"):
                            mode["executable"] = False
                            mode["discovery_state"] = "BASE_CONTRACT_UNRESOLVED"

            if profile.bet_type:
                for mode in modes:
                    request = mode.get("request")
                    if isinstance(request, dict):
                        request["bet_type"] = profile.bet_type

            if profile.custom_req and modes:
                base = modes[0]
                if not str(base.get("custom_req_profile") or ""):
                    base["custom_req_profile"] = profile.custom_profile
                    base["custom_req_literal_keys"] = sorted(profile.custom_literals)
                    if bool(base.get("executable")):
                        base["discovery_state"] = "OBSERVED_ENGINE_CONTRACT"
                        base["source"] = "live-game-bundle+engine-contract"

            return modes

        def observed_result_summary(data: dict[str, Any]):
            return _engine_result_summary(data, original_result_summary(data))

        def observed_rpc(
            runtime,
            method: str,
            *,
            timeout_s: float,
            params: dict[str, Any],
            rpc_id: int | str | None = None,
        ):
            adapted_params = params
            profile = _profiles.get(id(runtime))
            if method == "play" and profile is not None:
                adapted_params = apply_observed_play_wire(adapted_params, profile)

            result = original_rpc(
                runtime,
                method,
                timeout_s=timeout_s,
                params=adapted_params,
                rpc_id=rpc_id,
            )

            if method == "init":
                profile = _profiles.setdefault(id(runtime), ObservedHyperHiveWire())
                try:
                    response_data = result[2]
                    init_result = response_data.get("result")
                    attrs = (
                        init_result.get("currency_attributes")
                        if isinstance(init_result, dict)
                        else None
                    )
                    exponent = attrs.get("exponent") if isinstance(attrs, dict) else None
                    if isinstance(exponent, int):
                        profile.exponent = exponent
                except Exception:
                    pass
            return result

        hyperhive._download_engine_contract = observed_download_engine_contract
        hyperhive.discover_modes_from_bundle = observed_discover_modes
        hyperhive._result_summary = observed_result_summary
        hyperhive._rpc = observed_rpc
        _installed = True


__all__ = [
    "ObservedHyperHiveWire",
    "analyze_engine_wire",
    "apply_observed_play_wire",
    "install_observed_wire_adapter",
]
