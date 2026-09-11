from __future__ import annotations

import re

from tester_spin.providers.rubyplay import runtime as _runtime


# Imported only after runtime_contracts + bootstrap_fallback have been installed.
_ORIGINAL_DISCOVER = _runtime.discover_client_profile
_ORIGINAL_BOOTSTRAP = _runtime.bootstrap_game

_WRAPPER_REF = re.compile(
    r"(?:[A-Za-z_$][A-Za-z0-9_$]*\.)?_\$wrappers\[(\d+)\]\.getName\(\)"
)
_WRAPPER_CHAIN = re.compile(
    r"(?:[A-Za-z_$][A-Za-z0-9_$]*\.)?_\$wrappers\[\d+\]\.getName\(\)"
    r"(?:\s*\+\s*['\"]_['\"]\s*\+\s*"
    r"(?:[A-Za-z_$][A-Za-z0-9_$]*\.)?_\$wrappers\[\d+\]\.getName\(\))+"
)


def _action_wrapper_map(bundle: str) -> dict[int, str]:
    """Resolve Action._$wrappers indices without depending on game names."""
    by_index: dict[int, set[str]] = {}
    for raw_index, name in re.findall(
        r"new\s+[A-Za-z_$][A-Za-z0-9_$]*"
        r"\((\d+),['\"][^'\"]+['\"],['\"]([^'\"]+)['\"],",
        bundle or "",
    ):
        by_index.setdefault(int(raw_index), set()).add(str(name))
    return {
        index: next(iter(names))
        for index, names in by_index.items()
        if len(names) == 1
    }


def _feature_types_from_method(body: str, action_map: dict[int, str]) -> set[str]:
    """Resolve simple and composite Action-wrapper names from one method body.

    RubyPlay uses both a single wrapper (``freespin``) and concatenated wrappers
    such as ``freespin_respin``. Composite types are built literally as
    ``wrapper[2].getName()+"_"+wrapper[3].getName()``. We preserve that provider
    composition instead of inventing a per-game mapping.
    """
    candidates: set[str] = set()
    covered: list[tuple[int, int]] = []

    for match in _WRAPPER_CHAIN.finditer(body or ""):
        indexes = [int(raw) for raw in _WRAPPER_REF.findall(match.group(0))]
        names = [action_map.get(index, "") for index in indexes]
        if indexes and all(names):
            candidates.add("_".join(names))
            covered.append(match.span())

    def in_chain(position: int) -> bool:
        return any(start <= position < end for start, end in covered)

    for match in _WRAPPER_REF.finditer(body or ""):
        if in_chain(match.start()):
            continue
        name = action_map.get(int(match.group(1)), "")
        if name:
            candidates.add(name)

    return candidates


def _recover_buy_feature_type(bundle: str, profile) -> None:
    """Recover Buy Feature type from method-local Action wrapper references.

    Older discovery required ``isBuyFeatureGame`` to appear shortly after
    ``getBuyFeatureType``. Real RubyPlay bundles can place those methods hundreds
    of kilobytes apart. The method itself is authoritative: if every concrete
    implementation converges on one provider action/type string, that type is
    safe to use.
    """
    if str(getattr(profile, "buy_feature_type", "") or "").strip():
        return

    action_map = _action_wrapper_map(bundle)
    if not action_map:
        return

    candidates: set[str] = set()
    for match in re.finditer(
        r"getBuyFeatureType\([^)]*\)\{(?P<body>[^{}]{0,6000})\}",
        bundle or "",
        re.S,
    ):
        method_types = _feature_types_from_method(match.group("body"), action_map)
        if len(method_types) == 1:
            candidates.update(method_types)
        elif len(method_types) > 1:
            # A method that can resolve to multiple unrelated types is ambiguous;
            # do not guess which branch is active.
            return

    if len(candidates) == 1:
        profile.buy_feature_type = next(iter(candidates))
        profile.evidence.append(
            "client.getBuyFeatureType->Action wrapper (method-local)"
        )


def _client_buy_feature_capability(bundle: str, profile) -> bool | None:
    """Resolve whether the active client family actually implements Buy Feature.

    Positive evidence is a complete type+multiplier contract or a concrete
    ``isBuyFeatureGame(){return true}`` implementation. Legacy base classes often
    contain ``return false`` while the generic UI wrapper calls the session API
    through optional chaining; those false/base implementations must not be
    mistaken for an enabled feature.
    """
    if (
        str(getattr(profile, "buy_feature_type", "") or "").strip()
        and isinstance(getattr(profile, "buy_feature_multiplier", None), (int, float))
        and not isinstance(profile.buy_feature_multiplier, bool)
        and float(profile.buy_feature_multiplier) > 0
    ):
        return True

    generic_wrapper_seen = False
    concrete_true = False
    concrete_false = False
    unresolved_concrete = False

    for match in re.finditer(
        r"isBuyFeatureGame\([^)]*\)\{(?P<body>[^}]{0,1200})\}",
        bundle or "",
        re.S,
    ):
        compact = re.sub(r"\s+", "", match.group("body")).rstrip(";")
        if "getSession().isBuyFeatureGame?.()" in compact:
            generic_wrapper_seen = True
            continue
        if compact in {"return!0", "returntrue"}:
            concrete_true = True
            continue
        if compact in {"return!1", "returnfalse"}:
            concrete_false = True
            continue
        unresolved_concrete = True

    if concrete_true:
        return True
    if generic_wrapper_seen and not unresolved_concrete and not concrete_true:
        return False
    if concrete_false and not unresolved_concrete:
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

    _recover_buy_feature_type(bundle, profile)
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
    """Keep the raw server flag as telemetry but expose effective capability."""
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
