from __future__ import annotations

import re
from typing import Any

from tester_spin.providers.rubyplay import runtime as _runtime


# Imported only after runtime_contracts + bootstrap_fallback have been installed.
_ORIGINAL_DISCOVER = _runtime.discover_client_profile
_ORIGINAL_BOOTSTRAP = _runtime.bootstrap_game


def _client_buy_feature_capability(bundle: str, profile) -> bool | None:
    """Resolve whether the active client family actually implements Buy Feature.

    Some legacy RubyPlay servers advertise ``buy_feature_available=true`` even
    though the generated game session exposes no Buy Feature API at all. The UI's
    generic wrapper uses optional chaining precisely for this case:

        getSession().isBuyFeatureGame?.()

    We only return False when the bundle contains that generic wrapper but no
    session-side ``isBuyFeatureGame`` implementation anywhere. That is strong
    negative evidence. If a concrete buy type+multiplier was discovered, support
    is positively proven. Anything in-between remains None/conservative.
    """
    if (
        str(getattr(profile, "buy_feature_type", "") or "").strip()
        and isinstance(getattr(profile, "buy_feature_multiplier", None), (int, float))
        and float(profile.buy_feature_multiplier) > 0
    ):
        return True

    implementations: list[str] = []
    generic_wrapper_seen = False
    for match in re.finditer(
        r"isBuyFeatureGame\([^)]*\)\{(?P<body>[^}]{0,1200})\}",
        bundle or "",
        re.S,
    ):
        body = match.group("body")
        compact = re.sub(r"\s+", "", body)
        if "getSession().isBuyFeatureGame?.()" in compact:
            generic_wrapper_seen = True
            continue
        implementations.append(body)

    if generic_wrapper_seen and not implementations:
        return False
    return None


def discover_client_profile(scripts: list[tuple[str, str]]):
    profile = _ORIGINAL_DISCOVER(scripts)
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

    capability = _client_buy_feature_capability(bundle, profile)
    profile.buy_feature_client_supported = capability
    if capability is True:
        profile.evidence.append("client.buy-feature.contract=present")
    elif capability is False:
        profile.evidence.append("client.buy-feature.contract=absent")
    else:
        profile.evidence.append("client.buy-feature.contract=unresolved")
    return profile


def _apply_effective_buy_capability(runtime):
    """Keep the raw server flag as telemetry but expose effective capability.

    execution.py historically consumes ``data.buy_feature_available``. For a
    client-proven legacy false positive we retain the original server value under
    ``buy_feature_available_server`` and make the effective in-memory flag False.
    The raw init-response artifact has already been written by bootstrap and is
    therefore never falsified on disk.
    """
    profile = runtime.client_profile
    if profile.buy_feature_client_supported is not False:
        return runtime
    data = runtime.init_data.get("data")
    if not isinstance(data, dict):
        return runtime
    if data.get("buy_feature_available") is True:
        data["buy_feature_available_server"] = True
        data["buy_feature_available"] = False
        data["buy_feature_available_effective_reason"] = (
            "client_session_contract_absent"
        )
    return runtime


def bootstrap_game(
    session,
    public_url: str,
    *,
    timeout_s: float,
    cached_profile=None,
    artifact_dir=None,
):
    runtime = _ORIGINAL_BOOTSTRAP(
        session,
        public_url,
        timeout_s=timeout_s,
        cached_profile=cached_profile,
        artifact_dir=artifact_dir,
    )
    return _apply_effective_buy_capability(runtime)


def install_client_capability() -> None:
    from tester_spin.providers.rubyplay import execution as _execution

    _runtime.discover_client_profile = discover_client_profile
    _runtime.bootstrap_game = bootstrap_game
    _execution.bootstrap_game = bootstrap_game
