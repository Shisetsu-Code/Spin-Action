from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ObservedHyperHiveWire:
    bet_type: str = ""
    custom_req: bool = False
    custom_action: bool = False
    custom_exponent: bool = False
    custom_stake_on_spin: bool = False
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


def analyze_engine_wire(engine_contract: str) -> ObservedHyperHiveWire:
    """Extract only wire facts directly demonstrated by the loaded client JS.

    This intentionally does not route by game identifier/title. The patterns are
    structural contracts observed in HyperHive clients:

    * model property ``betType`` serialized as wire key ``bet_type``;
    * ``req.custom_req`` assigned from ``formattedRequest.params``;
    * action/exponent/stake copied into that formatted request.
    """
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

    return ObservedHyperHiveWire(
        bet_type="default" if default_bet_type else "",
        custom_req=custom_req,
        custom_action=custom_action,
        custom_exponent=custom_exponent,
        custom_stake_on_spin=custom_stake_on_spin,
    )


def apply_observed_play_wire(
    params: dict[str, Any],
    profile: ObservedHyperHiveWire,
) -> dict[str, Any]:
    """Return a play params copy adapted only with observed client evidence."""
    out = dict(params)
    raw_req = out.get("req")
    if not isinstance(raw_req, dict):
        return out
    req = dict(raw_req)

    if profile.bet_type:
        # The generated SugarMix-style model serializes ``betType=default`` as
        # ``bet_type=default``. Do not globally replace normal PZ ``bet`` clients.
        req["bet_type"] = profile.bet_type

    if profile.custom_req and "custom_req" not in req:
        action = str(req.pop("action", "") or "spin")
        custom: dict[str, Any] = {}
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


_install_lock = threading.Lock()
_installed = False
_profiles: dict[int, ObservedHyperHiveWire] = {}


def install_observed_wire_adapter() -> None:
    """Install an idempotent provider-local adapter around HyperHive helpers.

    ``run_hyperhive_test`` remains the authority for execution and validation.
    This adapter only fills fields whose serialization is directly proven by the
    downloaded engine contract. It exists separately so API-v2/legacy/switchable
    behavior is untouched.
    """
    global _installed
    with _install_lock:
        if _installed:
            return

        from tester_spin.providers.bgaming import hyperhive

        original_download_engine_contract = hyperhive._download_engine_contract
        original_discover_modes = hyperhive.discover_modes_from_bundle
        original_rpc = hyperhive._rpc

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

            if profile.bet_type:
                for mode in modes:
                    request = mode.get("request")
                    if isinstance(request, dict):
                        request["bet_type"] = profile.bet_type

            if profile.custom_req and modes:
                base = modes[0]
                if not str(base.get("custom_req_profile") or ""):
                    base["custom_req_profile"] = profile.custom_profile
                    base["discovery_state"] = "OBSERVED_ENGINE_CONTRACT"
                    base["source"] = "game_bundle_source+engine_contract"
            return modes

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
                adapted_params = apply_observed_play_wire(params, profile)

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

        hyperhive._download_engine_contract = observed_download_engine_contract
        hyperhive.discover_modes_from_bundle = observed_discover_modes
        hyperhive._rpc = observed_rpc
        _installed = True


__all__ = [
    "ObservedHyperHiveWire",
    "analyze_engine_wire",
    "apply_observed_play_wire",
    "install_observed_wire_adapter",
]
