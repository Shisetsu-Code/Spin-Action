from __future__ import annotations

import threading
from typing import Any

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
import tester_spin.providers.bgaming_exhaustive as _exhaustive
import tester_spin.providers.bgaming_path_policy as _policy
from tester_spin.providers.bgaming import execution as _execution
from tester_spin.providers.bgaming import runtime as _runtime


_policy.install_policy(_exhaustive)


def _coverage_active() -> bool:
    return bool(getattr(_policy._LOCAL, "coverage_active", False))


def _profile_guard(*args, **kwargs):
    """Apply policy discovery only inside the exhaustive wrapper run.

    Importing the public provider registry must not mutate the behavior of the
    lower-level BGaming adapter used by focused protocol tests/tools.
    """
    profile = _policy._ORIGINAL_DISCOVER_PROFILE(*args, **kwargs)
    if _coverage_active():
        return _policy._remember_profile(profile)
    return profile


def _purchase_mode_authorized(mode: dict[str, Any]) -> bool:
    explicit = getattr(_policy._LOCAL, "client_purchase_features", None)
    explicit_features = explicit if isinstance(explicit, set) else set()
    name = str(mode.get("name") or "")
    level = mode.get("level")

    if _policy._purchase_is_client_proven(name, explicit_features):
        return True

    dynamic = bool(
        getattr(_policy._LOCAL, "dynamic_purchased_feature", False)
    )
    level_supported = bool(
        getattr(_policy._LOCAL, "purchase_feature_level_supported", False)
    )
    return bool(dynamic and (level is None or level_supported))


def _purchase_modes_guard(data: dict[str, Any]) -> list[dict[str, Any]]:
    advertised = _policy._ORIGINAL_DISCOVER_PURCHASE_MODES(data)
    if not _coverage_active():
        return advertised

    # If profile discovery was explicitly replaced by a caller/test, there is
    # no policy authority context. Preserve legacy behavior instead of silently
    # deleting modes. Normal exhaustive runs always populate this context.
    explicit = getattr(_policy._LOCAL, "client_purchase_features", None)
    dynamic_seen = hasattr(_policy._LOCAL, "dynamic_purchased_feature")
    if not isinstance(explicit, set) and not dynamic_seen:
        return advertised

    return [mode for mode in advertised if _purchase_mode_authorized(mode)]


def _bet_guard(data: dict[str, Any]):
    if _coverage_active():
        return _policy.resolve_coverage_bet(data)
    return _policy._ORIGINAL_RESOLVE_BASE_BET(data)


# install_policy() supplies the HyperHive hook and scoped exhaustive choice
# domains. These guards keep the API-v2 monkey patches inert outside a wrapper
# run, so importing tester_spin.providers cannot alter the base adapter globally.
_execution.discover_profile = _profile_guard
_execution.discover_purchase_modes = _purchase_modes_guard
_execution.resolve_base_bet = _bet_guard


# Extend profile capture with the two client-side dynamic-wire capabilities.
_original_remember_profile = _policy._remember_profile


def _remember_profile_with_dynamic(profile: Any) -> Any:
    remembered = _original_remember_profile(profile)
    _policy._LOCAL.dynamic_purchased_feature = bool(
        getattr(profile, "dynamic_purchased_feature", False)
    )
    _policy._LOCAL.purchase_feature_level_supported = bool(
        getattr(profile, "purchase_feature_level_supported", False)
    )
    return remembered


_policy._remember_profile = _remember_profile_with_dynamic


class BGamingProvider(_exhaustive.BGamingProvider):
    """BGaming exhaustive traversal with scoped purchase/wager policy.

    Every client/provider-demonstrated executable path is still traversed,
    across as many fresh sessions as state consumption requires. Coverage uses
    the minimum provider-advertised wager so expensive feature paths remain
    reachable, while the original default and full wager domain are preserved
    for later high-volume sampling and local-emulation classification.

    Purchase identity and purchase level are scoped to PURCHASE_* paths rather
    than multiplied globally across unrelated SPIN/purchase modes. A purchase
    is executable only when its concrete name is client-proven or the client
    proves a dynamic purchased_feature serializer; levelled purchases also
    require client proof of purchased_feature_level support.
    """

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        _policy.begin_policy_run()
        # Mark capability context as unresolved until normal profile discovery
        # proves it. This distinguishes "no client proof" from an external
        # caller replacing profile discovery entirely.
        _policy._LOCAL.dynamic_purchased_feature = False
        _policy._LOCAL.purchase_feature_level_supported = False
        try:
            result = super().test_game(
                game,
                spins=spins,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
            )
            return _policy.finalize_policy_artifacts(result)
        finally:
            _policy.end_policy_run()
            for name in (
                "dynamic_purchased_feature",
                "purchase_feature_level_supported",
            ):
                try:
                    delattr(_policy._LOCAL, name)
                except AttributeError:
                    pass


# Preserve the public provider boundary used by registry/wiring tests.
BGamingProvider.__module__ = "tester_spin.providers.bgaming"

__all__ = ["BGamingProvider"]
