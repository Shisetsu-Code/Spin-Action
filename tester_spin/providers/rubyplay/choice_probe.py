from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.rubyplay.choice_domains import (
    classify_probe_failure,
    probe_contiguous_index_domain,
)

_CONTINUATION_GUARD = 256
_INDEX_ACTIONS = {"select", "pick"}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sanitize_request(payload: dict[str, Any]) -> dict[str, Any]:
    clean = json.loads(json.dumps(payload))
    if "key" in clean:
        clean["key"] = "<redacted-session-key>"
    return clean


def _mode(result: GameTestResult, mode_id: str) -> dict[str, Any] | None:
    matches = [
        mode
        for mode in result.discovered_modes
        if isinstance(mode, dict) and str(mode.get("id") or "") == mode_id
    ]
    return matches[0] if len(matches) == 1 else None


def _cached_profile(provider, game: Game):
    try:
        path = provider.game_dir(game) / "game.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload.get("client_profile") if isinstance(payload, dict) else None
        from tester_spin.providers.rubyplay.runtime import RubyPlayClientProfile

        return RubyPlayClientProfile.from_dict(raw)
    except Exception:
        return None


class _CaptureSession:
    """Delegate a RubyPlay session while retaining the last parseable response."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.last_payload: dict[str, Any] | None = None

    def post(self, *args, **kwargs):
        self.last_payload = None
        response = self.inner.post(*args, **kwargs)
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            self.last_payload = payload
        return response

    def close(self):
        return self.inner.close()

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _next_unused(used: set[int]) -> int:
    value = 0
    while value in used:
        value += 1
    return value


def replay_index_probe(
    provider,
    game: Game,
    result: GameTestResult,
    *,
    parent_mode: str,
    action: str,
    index: int,
    timeout_s: float,
    stop_event: threading.Event,
    artifact_dir: Path,
    bootstrap_fn: Callable[..., Any] | None = None,
    post_action_fn: Callable[..., Any] | None = None,
    validate_fn: Callable[..., list[str]] | None = None,
) -> dict[str, Any]:
    """Replay one RubyPlay indexed option from a fresh session to terminal.

    Only the first occurrence of ``action`` is the probed choice. Later pick
    continuations use distinct deterministic indices so a successful probe proves
    that the candidate itself can participate in a clean terminal feature.
    """
    command = str(action or "").strip().lower()
    if command not in _INDEX_ACTIONS:
        return {"index": int(index), "outcome": "PROTOCOL_ERROR", "error": "unsupported indexed action"}
    if stop_event.is_set():
        return {"index": int(index), "outcome": "CANCELLED"}

    from tester_spin.providers.rubyplay import runtime as runtime_module
    from tester_spin.providers.rubyplay.runtime_contracts import STATE_CONTINUATIONS

    bootstrap = bootstrap_fn or runtime_module.bootstrap_game
    send = post_action_fn or runtime_module.post_action
    validate = validate_fn or runtime_module.validate_action_response

    mode = _mode(result, parent_mode)
    if mode is None:
        return {
            "index": int(index),
            "outcome": "PROTOCOL_ERROR",
            "error": f"parent mode {parent_mode!r} is not uniquely defined",
        }

    raw_session = provider._new_session()
    runtime = None
    capture = None
    warnings: list[str] = []
    target_reached = False
    same_action_occurrences = 0
    used_pick_indices: set[int] = set()
    wire_steps = 0

    try:
        runtime = bootstrap(
            raw_session,
            game.url,
            timeout_s=max(1.0, float(timeout_s)),
            cached_profile=_cached_profile(provider, game),
            artifact_dir=artifact_dir / "bootstrap",
        )
        capture = _CaptureSession(runtime.session)
        runtime.session = capture

        def send_and_store(
            provider_action: str,
            *,
            action_index: int | None = None,
            root: bool = False,
        ) -> None:
            nonlocal wire_steps
            capture.last_payload = None
            kwargs: dict[str, Any] = {"timeout_s": max(1.0, float(timeout_s))}
            if action_index is not None:
                kwargs["action_index"] = int(action_index)

            if root and provider_action == "spin":
                kwargs["bet"] = runtime.default_bet
            elif root and provider_action == "buy_feature":
                feature_type = str(mode.get("buy_feature_type") or "").strip().lower()
                price = mode.get("default_price")
                if not feature_type:
                    raise ValueError(f"{parent_mode}: missing buy_feature_type")
                if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
                    price = runtime_module.purchase_price(
                        runtime.default_bet,
                        wager=getattr(runtime.client_profile, "wager", None),
                        multiplier=getattr(runtime.client_profile, "buy_feature_multiplier", None),
                    )
                if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
                    raise ValueError(f"{parent_mode}: missing positive buy_feature_price")
                kwargs.update(
                    {
                        "bet": runtime.default_bet,
                        "buy_feature_type": feature_type,
                        "buy_feature_price": price,
                    }
                )

            response, request, data, previous_an = send(
                runtime,
                provider_action,
                **kwargs,
            )
            wire_steps += 1
            warnings.extend(
                validate(
                    data,
                    action=provider_action,
                    previous_an=previous_an,
                )
            )
            label = "root" if root else f"step-{wire_steps:03d}"
            _write_json(artifact_dir / f"{label}-request.json", _sanitize_request(request))
            _write_json(artifact_dir / f"{label}-response.json", data)
            _write_json(
                artifact_dir / f"{label}-meta.json",
                {
                    "status_code": getattr(response, "status_code", None),
                    "action": provider_action,
                    "action_index": action_index,
                    "next_action": getattr(runtime, "next_action", ""),
                },
            )

        kind = str(mode.get("kind") or "").upper()
        root_action = "buy_feature" if kind == "PURCHASE" else "spin" if kind == "SPIN" else ""
        if not root_action:
            return {
                "index": int(index),
                "outcome": "PROTOCOL_ERROR",
                "error": f"unsupported parent kind {kind!r}",
            }

        try:
            send_and_store(root_action, root=True)
        except BaseException as exc:
            failure = classify_probe_failure(
                exc,
                last_payload=(capture.last_payload if capture is not None else None),
                action=root_action,
            )
            if failure.get("outcome") == "SEMANTIC_REJECTION":
                failure["outcome"] = "PROTOCOL_ERROR"
            return {"index": int(index), "target_reached": False, **failure}

        for _ in range(_CONTINUATION_GUARD):
            if stop_event.is_set():
                return {
                    "index": int(index),
                    "outcome": "CANCELLED",
                    "target_reached": target_reached,
                }
            next_action = str(getattr(runtime, "next_action", "") or "").strip().lower()
            if next_action == "spin":
                if not target_reached:
                    return {
                        "index": int(index),
                        "outcome": "PROMPT_NOT_REACHED",
                        "target_reached": False,
                        "wire_steps": wire_steps,
                    }
                if warnings:
                    return {
                        "index": int(index),
                        "outcome": "PROTOCOL_ERROR",
                        "target_reached": True,
                        "wire_steps": wire_steps,
                        "warnings": list(dict.fromkeys(warnings)),
                    }
                return {
                    "index": int(index),
                    "outcome": "TERMINAL",
                    "target_reached": True,
                    "wire_steps": wire_steps,
                    "same_action_occurrences": same_action_occurrences,
                }
            if next_action not in STATE_CONTINUATIONS:
                return {
                    "index": int(index),
                    "outcome": "NONTERMINAL",
                    "target_reached": target_reached,
                    "wire_steps": wire_steps,
                    "next_action": next_action,
                }

            action_index: int | None = None
            forced_now = False
            if next_action == command:
                same_action_occurrences += 1
                if not target_reached:
                    target_reached = True
                    forced_now = True
                    action_index = int(index)
                    if command == "pick":
                        used_pick_indices.add(action_index)
                elif command == "pick":
                    action_index = _next_unused(used_pick_indices)
                    used_pick_indices.add(action_index)
                elif command == "select":
                    action_index = 0
            elif next_action == "pick":
                action_index = _next_unused(used_pick_indices)
                used_pick_indices.add(action_index)
            elif next_action == "select":
                action_index = 0

            try:
                send_and_store(next_action, action_index=action_index)
            except BaseException as exc:
                failure = classify_probe_failure(
                    exc,
                    last_payload=(capture.last_payload if capture is not None else None),
                    action=next_action,
                )
                if failure.get("outcome") == "SEMANTIC_REJECTION" and not forced_now:
                    failure["outcome"] = "PROTOCOL_ERROR"
                return {
                    "index": int(index),
                    "target_reached": target_reached,
                    "wire_steps": wire_steps,
                    "same_action_occurrences": same_action_occurrences,
                    **failure,
                }

        return {
            "index": int(index),
            "outcome": "NONTERMINAL",
            "target_reached": target_reached,
            "wire_steps": wire_steps,
            "error": f"continuation guard {_CONTINUATION_GUARD} reached",
        }
    except BaseException as exc:
        failure = classify_probe_failure(
            exc,
            last_payload=(capture.last_payload if capture is not None else None),
            action=command,
        )
        if failure.get("outcome") == "SEMANTIC_REJECTION":
            failure["outcome"] = "PROTOCOL_ERROR"
        return {"index": int(index), "target_reached": target_reached, **failure}
    finally:
        try:
            if runtime is not None:
                runtime.session.close()
            else:
                raw_session.close()
        except Exception:
            pass


def expand_rubyplay_index_domains(
    provider,
    game: Game,
    result: GameTestResult,
    *,
    observed: dict[tuple[str, str], set[int]],
    timeout_s: float,
    stop_event: threading.Event,
    progress,
    replay_fn: Callable[..., dict[str, Any]] = replay_index_probe,
    max_index: int = 32,
) -> GameTestResult:
    """Prove parent-scoped RubyPlay select/pick domains by isolated live replay."""
    summaries: list[dict[str, Any]] = []
    run_root = Path(str(result.run_dir or "")) if result.run_dir else None

    for (parent_mode, action), observed_indices in sorted(observed.items()):
        if stop_event.is_set():
            break
        if action not in _INDEX_ACTIONS:
            continue
        progress(
            f"[{game.name}] RubyPlay {parent_mode}/{action}: "
            "probando dominio de índices en sesiones frescas."
        )

        def probe(index: int) -> dict[str, Any]:
            artifact_dir = (
                run_root
                / "diagnostics"
                / "choice-domain-probes"
                / parent_mode
                / action
                / f"index-{index:03d}"
                if run_root is not None
                else Path("diagnostics") / "choice-domain-probes" / parent_mode / action / f"index-{index:03d}"
            )
            return replay_fn(
                provider,
                game,
                result,
                parent_mode=parent_mode,
                action=action,
                index=index,
                timeout_s=timeout_s,
                stop_event=stop_event,
                artifact_dir=artifact_dir,
            )

        proof = probe_contiguous_index_domain(probe, max_index=max_index)
        summary = {
            "parent_mode": parent_mode,
            "action": action,
            "observed_indices": sorted(int(value) for value in observed_indices),
            **proof,
        }
        summaries.append(summary)

        # Multiple select prompts under one parent need prefix-sensitive domains;
        # do not let a root/global boundary falsely close them.
        repeated_select = bool(
            action == "select"
            and any(
                int(row.get("same_action_occurrences") or 0) > 1
                for row in proof.get("probes", [])
                if isinstance(row, dict)
            )
        )
        if proof.get("state") != "PROVEN" or repeated_select:
            if repeated_select:
                summary["state"] = "UNRESOLVED"
                summary["reason"] = "multiple select prompts require prefix-sensitive coverage"
            continue

        mode_id = f"{parent_mode}__{action.upper()}_INDEX_DOMAIN"
        result.discovered_modes = [
            mode
            for mode in result.discovered_modes
            if not (
                isinstance(mode, dict)
                and str(mode.get("id") or "") == mode_id
                and str(mode.get("kind") or "").upper() == "INDEXED_CHOICE"
            )
        ]
        domain = [str(value) for value in proof.get("required_options", [])]
        result.discovered_modes.append(
            {
                "id": mode_id,
                "kind": "INDEXED_CHOICE",
                "parent": parent_mode,
                "observed": True,
                "executable": True,
                "wire_command": action,
                "observed_indices": sorted(
                    {int(value) for value in observed_indices}
                    | {int(value) for value in domain}
                ),
                "coverage_required": True,
                "branch_signature": f"RUBYPLAY:{parent_mode}:{action}:index-domain",
                "required_options": domain,
                "covered_options": list(domain),
                "required_samples": 1,
                "sample_counts": {value: 1 for value in domain},
                "boundary_index": proof.get("boundary_index"),
                "domain_authority": "isolated-live-server-boundary",
                "reason": (
                    "Every index below the boundary reached a clean terminal "
                    "fresh-session feature and the next index was explicitly "
                    "rejected by the RubyPlay protocol."
                ),
            }
        )
        progress(
            f"[{game.name}] RubyPlay {parent_mode}/{action}: "
            f"dominio demostrado={domain}, frontera={proof.get('boundary_index')}."
        )

    if run_root is not None:
        try:
            _write_json(
                run_root / "diagnostics" / "rubyplay-choice-domain-probes.json",
                {
                    "schema": "tester-spin/rubyplay-choice-domain-probes/v1",
                    "game": result.slug,
                    "domains": summaries,
                },
            )
        except OSError:
            pass
    return result


__all__ = ["expand_rubyplay_index_domains", "replay_index_probe"]
