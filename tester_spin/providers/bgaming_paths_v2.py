from __future__ import annotations

import threading
from typing import Any

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
import tester_spin.providers.bgaming_exhaustive as _exhaustive
import tester_spin.providers.bgaming_path_policy as _policy
from tester_spin.providers.bgaming import execution as _execution


_policy.install_policy(_exhaustive)

_original_save_profile = _execution.save_profile
_original_next_missing_choice = _exhaustive._next_missing_choice


def _coverage_active() -> bool:
    return bool(getattr(_policy._LOCAL, "coverage_active", False))


def _merge_command_options(target: dict[str, dict[str, Any]], source: Any) -> None:
    if not isinstance(source, dict):
        return
    for command, raw_options in source.items():
        if not isinstance(raw_options, dict):
            continue
        target.setdefault(str(command), {}).update(raw_options)


def _profile_guard(*args, **kwargs):
    """Apply policy discovery only inside the exhaustive wrapper run."""
    profile = _policy._ORIGINAL_DISCOVER_PROFILE(*args, **kwargs)
    if _coverage_active():
        remembered = _policy._remember_profile(profile)
        proven = getattr(_policy._LOCAL, "proven_command_options", None)
        if not isinstance(proven, dict):
            proven = {}
        _merge_command_options(proven, getattr(profile, "command_options", {}))
        _policy._LOCAL.proven_command_options = proven
        _policy._LOCAL.dynamic_purchased_feature = bool(
            getattr(profile, "dynamic_purchased_feature", False)
        )
        _policy._LOCAL.purchase_feature_level_supported = bool(
            getattr(profile, "purchase_feature_level_supported", False)
        )
        return remembered
    return profile


def _save_profile_guard(path, profile) -> None:
    if _coverage_active():
        proven = getattr(_policy._LOCAL, "proven_command_options", None)
        if isinstance(proven, dict):
            _merge_command_options(profile.command_options, proven)
    _original_save_profile(path, profile)


def _purchase_mode_authorized(mode: dict[str, Any]) -> bool:
    explicit = getattr(_policy._LOCAL, "client_purchase_features", None)
    explicit_features = explicit if isinstance(explicit, set) else set()
    name = str(mode.get("name") or "")
    level = mode.get("level")

    if _policy._purchase_is_client_proven(name, explicit_features):
        return True

    dynamic = bool(getattr(_policy._LOCAL, "dynamic_purchased_feature", False))
    level_supported = bool(
        getattr(_policy._LOCAL, "purchase_feature_level_supported", False)
    )
    return bool(dynamic and (level is None or level_supported))


def _purchase_modes_guard(data: dict[str, Any]) -> list[dict[str, Any]]:
    advertised = _policy._ORIGINAL_DISCOVER_PURCHASE_MODES(data)
    if not _coverage_active():
        return advertised

    explicit = getattr(_policy._LOCAL, "client_purchase_features", None)
    dynamic_seen = hasattr(_policy._LOCAL, "dynamic_purchased_feature")
    if not isinstance(explicit, set) and not dynamic_seen:
        return advertised

    return [mode for mode in advertised if _purchase_mode_authorized(mode)]


def _bet_guard(data: dict[str, Any]):
    if _coverage_active():
        return _policy.resolve_coverage_bet(data)
    return _policy._ORIGINAL_RESOLVE_BASE_BET(data)


def _next_missing_choice_guard(graph, attempted, repetitions: int = 1):
    """Retry under-sampled flow choices fairly until the module replay guard fires.

    A forced run may contain N ordinary spins while a selector appears only a
    handful of times.  Treating a path as permanently attempted after one run
    made rare branches impossible to sample to quota.  We therefore schedule
    every still-deficient target again, rotating by replay count so one rare
    branch cannot monopolize all retries.
    """
    if not _coverage_active():
        return _original_next_missing_choice(graph, attempted, repetitions)

    target_samples = max(1, int(repetitions))
    replay_counts = getattr(_policy._LOCAL, "choice_replay_counts", None)
    if not isinstance(replay_counts, dict):
        replay_counts = {}
        _policy._LOCAL.choice_replay_counts = replay_counts

    candidates = []
    for point in graph.values():
        scope = str(point.get("scope") or "")
        command = str(point.get("command") or "")
        prefix = tuple(str(value) for value in point.get("prefix") or ())
        counts = point.get("sample_counts")
        counts = counts if isinstance(counts, dict) else {}
        for raw_option in point.get("available") or ():
            option = str(raw_option)
            observed = int(counts.get(option, 0) or 0)
            if observed >= target_samples:
                continue
            target = (scope, command, (*prefix, option))
            candidates.append(
                (
                    int(replay_counts.get(target, 0)),
                    -(target_samples - observed),
                    target,
                )
            )

    if not candidates:
        return None
    _, _, target = min(
        candidates,
        key=lambda item: (
            item[0],
            item[1],
            item[2][0],
            item[2][1],
            len(item[2][2]),
            item[2][2],
        ),
    )
    replay_counts[target] = int(replay_counts.get(target, 0)) + 1
    return target


# install_policy() supplies the HyperHive wager hook and scoped option domains.
# These guards stay inert outside a normal exhaustive provider run.
_execution.discover_profile = _profile_guard
_execution.save_profile = _save_profile_guard
_execution.discover_purchase_modes = _purchase_modes_guard
_execution.resolve_base_bet = _bet_guard
_exhaustive._next_missing_choice = _next_missing_choice_guard


class BGamingProvider(_exhaustive.BGamingProvider):
    """BGaming exhaustive traversal with scoped purchase/wager policy."""

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
        _policy._LOCAL.dynamic_purchased_feature = False
        _policy._LOCAL.purchase_feature_level_supported = False
        _policy._LOCAL.proven_command_options = {}
        _policy._LOCAL.choice_replay_counts = {}
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
                "proven_command_options",
                "choice_replay_counts",
            ):
                try:
                    delattr(_policy._LOCAL, name)
                except AttributeError:
                    pass


BGamingProvider.__module__ = "tester_spin.providers.bgaming"

__all__ = ["BGamingProvider"]
