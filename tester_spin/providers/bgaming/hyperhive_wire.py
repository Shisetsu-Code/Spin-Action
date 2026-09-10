from __future__ import annotations

import re
import threading
import weakref
from dataclasses import dataclass, field
from typing import Any

from tester_spin.providers.bgaming.hyperhive_transport import prepare_hyperhive_client


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
    purchase_custom_variants: list[dict[str, Any]] = field(default_factory=list)
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
    if text in {"true", "!0"}:
        return True
    if text in {"false", "!1"}:
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
    scalar = r'(?:!0|!1|true|false|null|-?\d+(?:\.\d+)?|"[^"\\]{0,200}"|\'[^\'\\]{0,200}\')'

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


def _feature_buy_boolean_contract(
    compact: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Learn feature-buy selector flags from the live game serializer.

    BGaming game clients can build one purchased_feature at the transport layer
    while selecting different buy variants inside AdditionalData.params. We
    accept this only when the live code explicitly resets selector fields to
    false and switch branches explicitly set individual selectors to true.
    """
    candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for match in re.finditer(r"customizeFeatureBuyRequestData\([^)]*\)\{", compact):
        segment = compact[match.start() : match.start() + 3000]
        stop = segment.find("setRequestConfig")
        if stop > 0:
            segment = segment[:stop]

        false_keys = {
            key
            for key in re.findall(
                r"AdditionalData\.params\.([A-Za-z_$][A-Za-z0-9_$]*)=!1",
                segment,
            )
            if _safe_literal_key(key)
        }
        true_keys = {
            key
            for key in re.findall(
                r"AdditionalData\.params\.([A-Za-z_$][A-Za-z0-9_$]*)=!0",
                segment,
            )
            if _safe_literal_key(key)
        }
        selectable = sorted(false_keys & true_keys)
        if not selectable:
            continue

        defaults = {key: False for key in sorted(false_keys)}
        variants: list[dict[str, Any]] = []
        for selected in selectable:
            variant = dict(defaults)
            variant[selected] = True
            variants.append(variant)
        candidates.append((defaults, variants))

    if len(candidates) != 1:
        return {}, []
    return candidates[0]


def _client_action_enum_values(text: str) -> set[str]:
    """Extract continuation vocabulary from action enums in the live client.

    A server-provided nextAction is executable only when the same value also
    appears in a loaded client enum whose symbolic name describes a game action.
    This is stronger evidence than accepting arbitrary loose string literals.
    """
    out: set[str] = set()
    for symbolic, value in re.findall(
        r"(?:\.|\])([A-Z][A-Z0-9_]*)\s*=\s*[\"']([a-z][a-z0-9_\-]{1,80})[\"']",
        text or "",
    ):
        name = symbolic.upper()
        if not any(marker in name for marker in ("SPIN", "RESPIN", "JACKPOT", "BONUS")):
            continue
        out.add(str(value).casefold())
    return out


def _purchase_feature_names(text: str) -> set[str]:
    """Discover purchased_feature values from literal or enum assignments."""
    out = {
        value
        for value in re.findall(
            r"purchased_feature\s*[:=]\s*[\"']([A-Za-z0-9_\-]+)[\"']",
            text or "",
        )
        if value
    }
    out.update(
        value
        for value in re.findall(
            r"purchased_feature\s*=\s*[A-Za-z_$][A-Za-z0-9_$]*\.([A-Za-z0-9_]+)",
            text or "",
        )
        if value
    )
    return out


def _variant_suffix(variant: dict[str, Any], index: int) -> str:
    true_keys = [key for key, value in variant.items() if value is True]
    if len(true_keys) == 1:
        key = re.sub(r"(?<!^)(?=[A-Z])", "_", true_keys[0]).upper()
        return key
    return f"VARIANT_{index}"


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

    feature_defaults, feature_variants = _feature_buy_boolean_contract(compact)
    custom_literals = _formatted_request_literals(compact) if custom_req else {}
    if custom_req:
        for key, value in feature_defaults.items():
            custom_literals.setdefault(key, value)

    return ObservedHyperHiveWire(
        bet_type="default" if default_bet_type else "",
        custom_req=custom_req,
        custom_action=custom_action,
        custom_exponent=custom_exponent,
        custom_stake_on_spin=custom_stake_on_spin,
        custom_literals=custom_literals,
        purchase_custom_variants=feature_variants if custom_req else [],
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

    existing_custom = req.get("custom_req")
    if profile.custom_req and not isinstance(existing_custom, dict):
        action = str(req.pop("action", "") or "spin")
        # HAR/live-client evidence shows that request-map selector defaults such
        # as isNormalBuy/isSuperBuy belong to the initial spin only. Continuation
        # actions serialize just their action/exponent unless their own template
        # explicitly supplied additional fields.
        custom: dict[str, Any] = (
            dict(profile.custom_literals)
            if action.casefold() == "spin"
            else {}
        )
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
    elif profile.custom_req and isinstance(existing_custom, dict):
        # Existing custom_req can be a provider-discovered purchase variant.
        # Preserve every selector exactly, but refresh dynamic currency exponent.
        custom = dict(existing_custom)
        if profile.custom_exponent and "exponent" in custom:
            custom["exponent"] = int(profile.exponent)
        if (
            profile.custom_stake_on_spin
            and str(custom.get("action") or "").casefold() == "spin"
            and "stake" in custom
            and isinstance(req.get("bet"), (int, float))
        ):
            custom["stake"] = req["bet"]
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
# A Session is the lifecycle boundary for fresh BGaming credentials. Weak keys
# avoid both id(runtime) reuse bugs and unbounded retention after a run ends.
_profiles: weakref.WeakKeyDictionary[Any, ObservedHyperHiveWire] = weakref.WeakKeyDictionary()


def _get_profile(runtime: Any) -> ObservedHyperHiveWire | None:
    try:
        return _profiles.get(runtime.session)
    except TypeError:
        return None


def _set_profile(runtime: Any, profile: ObservedHyperHiveWire) -> None:
    try:
        _profiles[runtime.session] = profile
    except TypeError:
        pass


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
        original_discover_actions = hyperhive.discover_action_vocabulary
        original_rpc = hyperhive._rpc
        original_result_summary = hyperhive._result_summary

        def observed_download_engine_contract(
            runtime,
            *,
            timeout_s: float,
        ) -> str:
            # HyperHive's outer /hyperhive document is only a launcher. Load the
            # fresh inner /?token=<play_token> client first so runtime.script_urls
            # contains the game scripts that actually serialize method=play.
            try:
                prepare_hyperhive_client(runtime, timeout_s=timeout_s)
            except Exception:
                # Discovery remains fail-closed below. Do not authorize a wager
                # merely because the inner document could not be loaded.
                pass
            text = original_download_engine_contract(runtime, timeout_s=timeout_s)
            _set_profile(runtime, analyze_engine_wire(text))
            return text

        def observed_discover_actions(
            bundle_text: str,
            engine_contract: str = "",
        ) -> set[str]:
            combined = (bundle_text or "") + "\n" + (engine_contract or "")
            return (
                original_discover_actions(bundle_text, engine_contract)
                | _client_action_enum_values(combined)
            )

        def observed_discover_modes(
            runtime,
            *,
            timeout_s: float,
            bundle_text: str | None = None,
            engine_contract: str = "",
        ):
            # run_hyperhive_test downloads one bundle before engine discovery.
            # Engine discovery loads the inner iframe, so refresh the bundle here
            # to include scripts discovered from that live inner document.
            try:
                prepare_hyperhive_client(runtime, timeout_s=timeout_s)
            except Exception:
                pass
            resolved_bundle = hyperhive._download_bundle(runtime, timeout_s)
            if not resolved_bundle and bundle_text is not None:
                resolved_bundle = bundle_text

            modes = original_discover_modes(
                runtime,
                timeout_s=timeout_s,
                bundle_text=resolved_bundle,
                engine_contract=engine_contract,
            )
            profile = analyze_engine_wire(engine_contract)
            _set_profile(runtime, profile)
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
                        base["source"] = "live-inner-client+engine-contract"

            # Some HyperHive games expose one transport purchase feature (for
            # example buy_bonus) while the actual normal/super choice is encoded
            # by booleans inside custom_req. Learn those variants from the live
            # game's customizeFeatureBuyRequestData implementation.
            if modes and profile.purchase_custom_variants:
                feature_names = _purchase_feature_names(combined)
                mode_features = {
                    str((mode.get("request") or {}).get("purchased_feature") or "")
                    for mode in modes
                    if isinstance(mode.get("request"), dict)
                }
                candidate_features = sorted(
                    feature for feature in (feature_names | mode_features) if feature
                )
                # Ambiguous multiple purchase transports stay discovery-only;
                # do not guess which transport feature owns the boolean variants.
                if len(candidate_features) == 1:
                    feature = candidate_features[0]
                    filtered: list[dict[str, Any]] = []
                    for mode in modes:
                        request = mode.get("request")
                        if not isinstance(request, dict):
                            filtered.append(mode)
                            continue
                        if (
                            request.get("purchased_feature") == feature
                            and set(request).issubset({"bet_type", "purchased_feature"})
                        ):
                            continue
                        filtered.append(mode)
                    modes = filtered

                    base_bet_type = ""
                    if modes and isinstance(modes[0].get("request"), dict):
                        base_bet_type = str(modes[0]["request"].get("bet_type") or "")
                    for index, variant in enumerate(profile.purchase_custom_variants, 1):
                        custom_req = dict(variant)
                        if profile.custom_action:
                            custom_req["action"] = "spin"
                        if profile.custom_exponent:
                            custom_req["exponent"] = int(profile.exponent)
                        request: dict[str, Any] = {
                            "purchased_feature": feature,
                            "custom_req": custom_req,
                        }
                        if base_bet_type:
                            request["bet_type"] = base_bet_type
                        modes.append(
                            {
                                "id": (
                                    f"PURCHASE_{feature.upper()}_"
                                    f"{_variant_suffix(variant, index)}"
                                ),
                                "kind": "PURCHASE",
                                "request": request,
                                "expected_multiplier": None,
                                "source": "live-inner-client+feature-buy-contract",
                                "executable": bool(modes[0].get("executable")),
                                "discovery_state": "OBSERVED_CUSTOM_REQ_VARIANT",
                            }
                        )

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
            profile = _get_profile(runtime)
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
                profile = _get_profile(runtime)
                if profile is None:
                    profile = ObservedHyperHiveWire()
                    _set_profile(runtime, profile)
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
        hyperhive.discover_action_vocabulary = observed_discover_actions
        hyperhive._result_summary = observed_result_summary
        hyperhive._rpc = observed_rpc
        _installed = True


__all__ = [
    "ObservedHyperHiveWire",
    "analyze_engine_wire",
    "apply_observed_play_wire",
    "install_observed_wire_adapter",
]
