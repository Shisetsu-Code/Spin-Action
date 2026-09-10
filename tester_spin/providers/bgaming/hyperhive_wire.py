from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from tester_spin.providers.bgaming.hyperhive_har import (
    apply_har_play_wire,
    clear_thread_har_path,
    current_thread_har_evidence,
    set_thread_har_path,
)


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
    """Extract only scalar literals proven inside formattedRequest.params.

    HyperHive clients can construct custom_req through a formatted request object.
    Previous discovery only noticed action/exponent/stake and silently dropped
    literal protocol flags such as isNormalBuy=false / isSuperBuy=false. Those
    flags are part of the wire contract and omitting them can produce RPC 51100.
    """
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

    object_patterns = (
        r"formattedRequest\.params=\{([^{}]{1,1600})\}",
        r"formattedRequest=\{params:\{([^{}]{1,1600})\}",
    )
    for pattern in object_patterns:
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
    """Extract only wire facts directly demonstrated by the loaded client JS."""
    text = engine_contract or ""
    compact = re.sub(r"\s+", "", text)

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
    custom_literals = (
        _formatted_request_literals(compact)
        if custom_req
        else {}
    )

    return ObservedHyperHiveWire(
        bet_type="default" if default_bet_type else "",
        custom_req=custom_req,
        custom_action=custom_action,
        custom_exponent=custom_exponent,
        custom_stake_on_spin=custom_stake_on_spin,
        custom_literals=custom_literals,
    )


def apply_observed_play_wire(
    params: dict[str, Any],
    profile: ObservedHyperHiveWire,
) -> dict[str, Any]:
    """Return a play params copy adapted only with observed client JS evidence."""
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
    """Require direct evidence that bet is part of JSON-RPC params.req."""
    value = text or ""
    return bool(
        re.search(
            r'(?:\breq\s*:\s*\{[^{}]{0,1600}\bbet\s*:|\.req\.bet\s*=|\.req\[["\']bet["\']\]\s*=)',
            value,
        )
    )


def _has_req_bet_type_evidence(text: str) -> bool:
    """Distinguish a req-scoped bet_type from unrelated loose literals."""
    value = text or ""
    return bool(
        re.search(
            r'(?:\breq\s*:\s*\{[^{}]{0,1600}\bbet_type\s*:|\.req\.bet_type\s*=|\.req\[["\']bet_type["\']\]\s*=)',
            value,
        )
    )


_install_lock = threading.Lock()
_installed = False
_profiles: dict[int, ObservedHyperHiveWire] = {}


def _har_result_summary(data: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    """Fill response facts proven by observed HyperHive engine envelopes."""
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


def install_observed_wire_adapter() -> None:
    """Install provider-local adapters for JS and HAR-observed HyperHive wire."""
    global _installed
    with _install_lock:
        if _installed:
            return

        from tester_spin.providers.bgaming import hyperhive
        from tester_spin.providers.bgaming.adapter import BGamingProvider as BaseBGamingProvider
        from tester_spin.providers.bgaming.har_capture import append_har_debug
        from tester_spin.providers.bgaming.har_select import inspect_har, select_best_har

        original_download_engine_contract = hyperhive._download_engine_contract
        original_discover_modes = hyperhive.discover_modes_from_bundle
        original_discover_actions = hyperhive.discover_action_vocabulary
        original_rpc = hyperhive._rpc
        original_rpc_id = hyperhive._hyperhive_rpc_id
        original_result_summary = hyperhive._result_summary
        original_provider_test_game = BaseBGamingProvider.test_game

        def har_context_test_game(self, game, **kwargs):
            game_dir = self.game_dir(game)
            har_path = select_best_har(game_dir)
            quality = inspect_har(har_path)
            progress = kwargs.get("progress")
            if har_path is not None:
                try:
                    relative = har_path.relative_to(game_dir)
                except ValueError:
                    relative = har_path
                append_har_debug(
                    game_dir,
                    "runner_har_selected",
                    path=str(relative),
                    quality=(quality.grade if quality is not None else "UNKNOWN"),
                    operations=(quality.operations if quality is not None else 0),
                    plays=(quality.plays if quality is not None else 0),
                    spins=(quality.spins if quality is not None else 0),
                    purchases=(quality.purchases if quality is not None else 0),
                )
                if callable(progress) and quality is not None:
                    progress(
                        f"[{game.name}] HAR usado por runner: {relative}; "
                        f"calidad={quality.grade}, plays={quality.plays}, "
                        f"spins={quality.spins}, compras={quality.purchases}."
                    )
            else:
                append_har_debug(game_dir, "runner_har_missing")
                if callable(progress):
                    progress(f"[{game.name}] HAR usado por runner: ninguno.")

            set_thread_har_path(har_path)
            try:
                return original_provider_test_game(self, game, **kwargs)
            finally:
                clear_thread_har_path()

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
            modes = original_discover_modes(
                runtime,
                timeout_s=timeout_s,
                bundle_text=bundle_text,
                engine_contract=engine_contract,
            )
            profile = analyze_engine_wire(engine_contract)
            _profiles[id(runtime)] = profile
            har = current_thread_har_evidence()
            combined = (bundle_text or "") + "\n" + (engine_contract or "")
            req_bet_observed = _has_req_bet_evidence(combined)
            req_bet_type_observed = _has_req_bet_type_evidence(combined)

            # The legacy discovery function historically injected bet_type="bet"
            # and marked SPIN executable even with no req evidence. Enforce the
            # documented fail-closed contract at the active provider boundary:
            # without a HAR play or client proof of params.req.bet, no wager is sent.
            if modes:
                base = modes[0]
                request = base.get("request")
                if isinstance(request, dict):
                    if (
                        request.get("bet_type") == "bet"
                        and not req_bet_type_observed
                        and not profile.bet_type
                        and not har.bet_type
                    ):
                        request.pop("bet_type", None)
                if not har.usable and not req_bet_observed:
                    base["executable"] = False
                    base["discovery_state"] = "CONTRACT_UNRESOLVED"
                    base["source"] = "client-evidence-incomplete"
                    for mode in modes[1:]:
                        if mode.get("kind") == "PURCHASE" and mode.get("executable"):
                            mode["executable"] = False
                            mode["discovery_state"] = "BASE_CONTRACT_UNRESOLVED"

            if profile.bet_type and not har.bet_type:
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
                        base["source"] = "game_bundle_source+engine_contract"

            if har.usable and modes:
                base = modes[0]
                if har.bet_type:
                    for mode in modes:
                        request = mode.get("request")
                        if isinstance(request, dict):
                            request["bet_type"] = har.bet_type
                base["executable"] = True
                base["discovery_state"] = "HAR_OBSERVED_WIRE"
                base["source"] = "observed-har-play"
                if har.spin is not None and har.spin.custom_req:
                    base["custom_req_profile"] = "har-observed"
                    base["custom_req_literal_keys"] = sorted(har.spin.custom_req)

                for feature in sorted(har.purchase_features):
                    existing = None
                    for mode in modes:
                        request = mode.get("request")
                        if (
                            isinstance(request, dict)
                            and str(request.get("purchased_feature") or "") == feature
                        ):
                            existing = mode
                            break
                    if existing is None:
                        request = {"purchased_feature": feature}
                        if har.bet_type:
                            request["bet_type"] = har.bet_type
                        existing = {
                            "id": f"PURCHASE_{feature.upper()}",
                            "kind": "PURCHASE",
                            "request": request,
                            "expected_multiplier": None,
                            "source": "observed-har-play",
                            "executable": True,
                            "discovery_state": "HAR_OBSERVED_WIRE",
                        }
                        modes.append(existing)
                    else:
                        existing["source"] = "observed-har-play"
                        existing["executable"] = True
                        existing["discovery_state"] = "HAR_OBSERVED_WIRE"
            return modes

        def observed_discover_actions(bundle_text: str, engine_contract: str = ""):
            actions = set(original_discover_actions(bundle_text, engine_contract))
            har = current_thread_har_evidence()
            actions.update(har.actions)
            return actions

        def observed_rpc_id(engine_contract: str):
            har = current_thread_har_evidence()
            if har.rpc_id_profile == "zero":
                return 0
            if har.rpc_id_profile == "uuid":
                return str(uuid.uuid4())
            return original_rpc_id(engine_contract)

        def observed_result_summary(data: dict[str, Any]):
            summary = original_result_summary(data)
            return _har_result_summary(data, summary)

        def observed_rpc(
            runtime,
            method: str,
            *,
            timeout_s: float,
            params: dict[str, Any],
            rpc_id: int | str | None = None,
        ):
            profile = _profiles.get(id(runtime))
            adapted_params = params
            if method == "play" and profile is not None:
                adapted_params = apply_observed_play_wire(adapted_params, profile)
            if method == "play":
                har = current_thread_har_evidence()
                if har.usable:
                    adapted_params = apply_har_play_wire(adapted_params, har)

            result = original_rpc(
                runtime,
                method,
                timeout_s=timeout_s,
                params=adapted_params,
                rpc_id=rpc_id,
            )

            if method == "init":
                profile = _profiles.setdefault(
                    id(runtime),
                    ObservedHyperHiveWire(),
                )
                try:
                    data = result[2]
                    init_result = data.get("result")
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

        BaseBGamingProvider.test_game = har_context_test_game
        hyperhive._download_engine_contract = observed_download_engine_contract
        hyperhive.discover_modes_from_bundle = observed_discover_modes
        hyperhive.discover_action_vocabulary = observed_discover_actions
        hyperhive._hyperhive_rpc_id = observed_rpc_id
        hyperhive._result_summary = observed_result_summary
        hyperhive._rpc = observed_rpc
        _installed = True


__all__ = [
    "ObservedHyperHiveWire",
    "analyze_engine_wire",
    "apply_observed_play_wire",
    "install_observed_wire_adapter",
]
