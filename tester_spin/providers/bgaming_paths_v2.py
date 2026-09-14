from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
import tester_spin.providers.bgaming_exhaustive as _exhaustive
import tester_spin.providers.bgaming_path_policy as _policy
from tester_spin.providers.bgaming import execution as _execution
from tester_spin.providers.bgaming.server_guided import (
    begin_dynamic_contract_run,
    discover_server_guided_client_evidence,
    end_dynamic_contract_run,
    remember_dynamic_evidence,
)


_policy.install_policy(_exhaustive)

_original_save_profile = _execution.save_profile
_original_post_command = _execution.post_command
_original_next_missing_choice = _exhaustive._next_missing_choice
_original_move_run = _exhaustive._move_run


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


def _evidence_key(value: dict[str, Any]) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _post_command_guard(
    runtime,
    command: str,
    *,
    timeout_s: float,
    options: dict[str, Any] | None = None,
    extra_data: dict[str, Any] | None = None,
):
    result = _original_post_command(
        runtime,
        command,
        timeout_s=timeout_s,
        options=options,
        extra_data=extra_data,
    )
    if not _coverage_active():
        return result

    try:
        data = result[2]
        evidence = discover_server_guided_client_evidence(
            runtime,
            data,
            timeout_s=timeout_s,
        )
        if isinstance(evidence, dict):
            # Promotion remains narrow: only exact client-proven, fully literal
            # payload variants become replay candidates. Text hits alone never do.
            remember_dynamic_evidence(evidence)
            from tester_spin.providers.bgaming.structural_map import record_discovery
            record_discovery(data, evidence)

            items = getattr(_policy._LOCAL, "server_guided_evidence", None)
            if not isinstance(items, list):
                items = []
                _policy._LOCAL.server_guided_evidence = items
            seen = getattr(_policy._LOCAL, "server_guided_seen", None)
            if not isinstance(seen, set):
                seen = set()
                _policy._LOCAL.server_guided_seen = seen
            key = _evidence_key(evidence)
            if key not in seen:
                seen.add(key)
                items.append(evidence)
    except Exception as exc:
        diagnostics = getattr(_policy._LOCAL, "server_guided_errors", None)
        if not isinstance(diagnostics, list):
            diagnostics = []
            _policy._LOCAL.server_guided_errors = diagnostics
        message = f"{type(exc).__name__}: {exc}"
        if message not in diagnostics:
            diagnostics.append(message[:800])
    return result


def _write_server_guided_artifact(result: GameTestResult) -> None:
    run_dir = Path(str(result.run_dir or ""))
    if not run_dir.is_dir():
        return
    evidence = getattr(_policy._LOCAL, "server_guided_evidence", None)
    errors = getattr(_policy._LOCAL, "server_guided_errors", None)
    rows = list(evidence) if isinstance(evidence, list) else []
    error_rows = list(errors) if isinstance(errors, list) else []
    if not rows and not error_rows:
        return
    payload = {
        "schema": "tester-spin/bgaming-server-guided-run/v1",
        "provider": "bgaming",
        "game": result.slug,
        "states": rows,
        "diagnostics": error_rows,
        "execution_policy": (
            "server advertisement or textual client hits alone do not authorize "
            "unknown requests; only exact client-proven fully literal payload "
            "variants marked replay_eligible may be replayed"
        ),
    }
    path = run_dir / "server-guided-discovery.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
    """Retry under-sampled flow choices fairly until the replay guard fires."""
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


def _move_run_guard(result: GameTestResult, target: Path) -> None:
    """Never overwrite evidence when a sparse branch needs another replay."""
    if not _coverage_active() or not target.exists():
        _original_move_run(result, target)
        return
    index = 2
    while True:
        candidate = target.with_name(f"{target.name}-sample-{index:03d}")
        if not candidate.exists():
            _original_move_run(result, candidate)
            return
        index += 1


# install_policy() supplies the HyperHive wager hook and scoped option domains.
# These guards stay inert outside a normal exhaustive provider run.
_execution.discover_profile = _profile_guard
_execution.save_profile = _save_profile_guard
_execution.post_command = _post_command_guard
_execution.discover_purchase_modes = _purchase_modes_guard
_execution.resolve_base_bet = _bet_guard
_exhaustive._next_missing_choice = _next_missing_choice_guard
_exhaustive._move_run = _move_run_guard


class BGamingProvider(_exhaustive.BGamingProvider):
    """BGaming exhaustive traversal with scoped purchase/wager policy."""

    # The active BGaming adapter uses per-game HTTP sessions and thread-local
    # policy/HAR context, so let the shared scheduler honor the GUI-requested
    # concurrency instead of forcing all BGaming games through one worker.
    max_test_concurrency = None

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        from tester_spin.providers.bgaming.structural_map import begin_capture, end_capture
        from tester_spin.structure import atomic_write
        capture, capture_token = begin_capture(game.slug)
        _policy.begin_policy_run()
        begin_dynamic_contract_run()
        _policy._LOCAL.dynamic_purchased_feature = False
        _policy._LOCAL.purchase_feature_level_supported = False
        _policy._LOCAL.proven_command_options = {}
        _policy._LOCAL.choice_replay_counts = {}
        _policy._LOCAL.server_guided_evidence = []
        _policy._LOCAL.server_guided_seen = set()
        _policy._LOCAL.server_guided_errors = []
        try:
            result = super().test_game(
                game,
                spins=spins,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
            )
            _write_server_guided_artifact(result)
            result = _policy.finalize_policy_artifacts(result)
            try:
                capture.finish(result, self.game_dir(game))
            except Exception as exc:
                result.structural_map = {"status": "failed", "diagnostic": type(exc).__name__}
                if result.status == "OK":
                    result.status = "PARCIAL"
                result.error = (str(result.error or "") + " Structural Map: " + type(exc).__name__).strip()
            if result.run_dir and Path(result.run_dir).is_dir():
                try:
                    atomic_write(Path(result.run_dir) / "result.json", result.to_dict())
                except OSError as exc:
                    result.structural_map["result_write_diagnostic"] = type(exc).__name__
                    if result.status == "OK":
                        result.status = "PARCIAL"
            return result
        finally:
            end_capture(capture_token)
            end_dynamic_contract_run()
            _policy.end_policy_run()
            for name in (
                "dynamic_purchased_feature",
                "purchase_feature_level_supported",
                "proven_command_options",
                "choice_replay_counts",
                "server_guided_evidence",
                "server_guided_seen",
                "server_guided_errors",
            ):
                try:
                    delattr(_policy._LOCAL, name)
                except AttributeError:
                    pass


BGamingProvider.__module__ = "tester_spin.providers.bgaming"

__all__ = ["BGamingProvider"]
