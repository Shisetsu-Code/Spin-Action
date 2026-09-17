from __future__ import annotations

import hashlib
import json
import re
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
_STEP_REQUEST_RE = re.compile(r"^step-(\d+)-request\.json$")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


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


def _ordered_requests(root: Path) -> list[dict[str, Any]]:
    rows: list[tuple[int, dict[str, Any]]] = []
    entry = root / "request.json"
    if entry.is_file():
        rows.append((0, _load_json(entry)))
    for path in root.glob("step-*-request.json"):
        match = _STEP_REQUEST_RE.fullmatch(path.name)
        if not match:
            continue
        rows.append((int(match.group(1)), _load_json(path)))
    return [payload for _step, payload in sorted(rows, key=lambda item: item[0])]


def _index_value(payload: dict[str, Any]) -> int | None:
    raw = payload.get("index")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _path_token(action: str, index: int) -> str:
    return f"{str(action).strip().lower()}={int(index)}"


def _parse_path_token(raw: Any) -> tuple[str, int] | None:
    text = str(raw or "").strip().lower()
    if "=" not in text:
        return None
    action, value = text.split("=", 1)
    if action not in _INDEX_ACTIONS:
        return None
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    if index < 0:
        return None
    return action, index


def observed_index_prompts(
    result: GameTestResult,
) -> dict[tuple[str, str, tuple[str, ...]], set[int]]:
    """Return indexed prompt points keyed by parent/action/path prefix.

    ``select`` prompts are path-sensitive: every prior indexed choice is encoded
    as an action-qualified token (for example ``select=1`` or ``pick=0``).
    Repeated ``pick`` messages intentionally share one root domain because RubyPlay
    pick chains consume distinct indices from the same initial option pool.
    """
    found: dict[tuple[str, str, tuple[str, ...]], set[int]] = {}
    for attempt in result.attempts:
        root = Path(str(attempt.artifact_dir or ""))
        if not root.is_dir():
            continue
        prefix: list[str] = []
        for payload in _ordered_requests(root):
            action = str(payload.get("action") or "").strip().lower()
            if action not in _INDEX_ACTIONS:
                continue
            index = _index_value(payload)
            if index is None:
                continue
            prompt_prefix = () if action == "pick" else tuple(prefix)
            key = (str(attempt.mode_id or "UNKNOWN"), action, prompt_prefix)
            found.setdefault(key, set()).add(index)
            prefix.append(_path_token(action, index))
    return found


def _prefix_digest(prefix: tuple[str, ...]) -> str:
    raw = json.dumps(list(prefix), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12].upper()


def _prefix_label(prefix: tuple[str, ...]) -> str:
    if not prefix:
        return "ROOT"
    readable = "__".join(
        re.sub(r"[^A-Za-z0-9._=-]+", "_", value)[:40]
        for value in prefix
    )[:120]
    return f"{readable}-{_prefix_digest(prefix)[:8]}"


def choice_domain_mode_id(parent_mode: str, action: str, prefix: tuple[str, ...] = ()) -> str:
    base = f"{parent_mode}__{str(action).upper()}_INDEX_DOMAIN"
    return base if not prefix else f"{base}__PREFIX_{_prefix_digest(prefix)}"


def choice_domain_signature(parent_mode: str, action: str, prefix: tuple[str, ...] = ()) -> str:
    encoded = json.dumps(list(prefix), ensure_ascii=False, separators=(",", ":"))
    return f"RUBYPLAY:{parent_mode}:{str(action).lower()}:index-domain:{encoded}"


class _CaptureSession:
    """Delegate a RubyPlay session while retaining the last request/response."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.last_request: dict[str, Any] | None = None
        self.last_payload: dict[str, Any] | None = None

    def post(self, *args, **kwargs):
        request = kwargs.get("json")
        self.last_request = dict(request) if isinstance(request, dict) else None
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


def _persist_failure(
    artifact_dir: Path,
    capture: _CaptureSession | None,
    failure: dict[str, Any],
) -> None:
    try:
        if capture is not None and isinstance(capture.last_request, dict):
            _write_json(
                artifact_dir / "failure-request.json",
                _sanitize_request(capture.last_request),
            )
        if capture is not None and isinstance(capture.last_payload, dict):
            _write_json(
                artifact_dir / "failure-response.json",
                capture.last_payload,
            )
        _write_json(artifact_dir / "failure.json", dict(failure))
    except OSError:
        pass


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
    prefix: tuple[str, ...] | list[str] = (),
    timeout_s: float,
    stop_event: threading.Event,
    artifact_dir: Path,
    bootstrap_fn: Callable[..., Any] | None = None,
    post_action_fn: Callable[..., Any] | None = None,
    validate_fn: Callable[..., list[str]] | None = None,
) -> dict[str, Any]:
    """Replay one RubyPlay indexed option from a fresh session to terminal.

    ``prefix`` encodes every indexed choice that must occur before the target
    prompt, using action-qualified tokens such as ``select=1``. Prefix actions are
    forced exactly; the candidate ``index`` is forced only at the next occurrence
    of ``action``. Later pick continuations use distinct deterministic indices.
    """
    command = str(action or "").strip().lower()
    normalized_prefix = tuple(str(value) for value in (prefix or ()))
    parsed_prefix: list[tuple[str, int]] = []
    for token in normalized_prefix:
        parsed = _parse_path_token(token)
        if parsed is None:
            return {
                "index": int(index),
                "prefix": list(normalized_prefix),
                "outcome": "PROTOCOL_ERROR",
                "error": f"invalid indexed prefix token: {token!r}",
            }
        parsed_prefix.append(parsed)

    if command not in _INDEX_ACTIONS:
        return {
            "index": int(index),
            "prefix": list(normalized_prefix),
            "outcome": "PROTOCOL_ERROR",
            "error": "unsupported indexed action",
        }
    if stop_event.is_set():
        return {
            "index": int(index),
            "prefix": list(normalized_prefix),
            "outcome": "CANCELLED",
        }

    from tester_spin.providers.rubyplay import runtime as runtime_module
    from tester_spin.providers.rubyplay.runtime_contracts import STATE_CONTINUATIONS

    bootstrap = bootstrap_fn or runtime_module.bootstrap_game
    send = post_action_fn or runtime_module.post_action
    validate = validate_fn or runtime_module.validate_action_response

    mode = _mode(result, parent_mode)
    if mode is None:
        return {
            "index": int(index),
            "prefix": list(normalized_prefix),
            "outcome": "PROTOCOL_ERROR",
            "error": f"parent mode {parent_mode!r} is not uniquely defined",
        }

    raw_session = provider._new_session()
    runtime = None
    capture = None
    warnings: list[str] = []
    target_reached = False
    same_action_occurrences = 0
    prefix_position = 0
    used_pick_indices: set[int] = set()
    wire_steps = 0

    def contextual(**payload: Any) -> dict[str, Any]:
        return {
            "index": int(index),
            "prefix": list(normalized_prefix),
            "target_reached": target_reached,
            **payload,
        }

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
            capture.last_request = None
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
                    "prefix": list(normalized_prefix),
                },
            )

        kind = str(mode.get("kind") or "").upper()
        root_action = "buy_feature" if kind == "PURCHASE" else "spin" if kind == "SPIN" else ""
        if not root_action:
            return contextual(
                outcome="PROTOCOL_ERROR",
                error=f"unsupported parent kind {kind!r}",
            )

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
            outcome = contextual(**failure)
            _persist_failure(artifact_dir, capture, outcome)
            return outcome

        for _ in range(_CONTINUATION_GUARD):
            if stop_event.is_set():
                return contextual(outcome="CANCELLED", wire_steps=wire_steps)

            next_action = str(getattr(runtime, "next_action", "") or "").strip().lower()
            if next_action == "spin":
                if not target_reached:
                    return contextual(
                        outcome="PROMPT_NOT_REACHED",
                        wire_steps=wire_steps,
                        prefix_consumed=prefix_position,
                    )
                if warnings:
                    return contextual(
                        outcome="PROTOCOL_ERROR",
                        wire_steps=wire_steps,
                        warnings=list(dict.fromkeys(warnings)),
                    )
                return contextual(
                    outcome="TERMINAL",
                    wire_steps=wire_steps,
                    same_action_occurrences=same_action_occurrences,
                    prefix_consumed=prefix_position,
                )

            if next_action not in STATE_CONTINUATIONS:
                return contextual(
                    outcome="NONTERMINAL",
                    wire_steps=wire_steps,
                    next_action=next_action,
                    prefix_consumed=prefix_position,
                )

            action_index: int | None = None
            forced_target = False
            if next_action in _INDEX_ACTIONS:
                if prefix_position < len(parsed_prefix):
                    expected_action, expected_index = parsed_prefix[prefix_position]
                    if next_action != expected_action:
                        return contextual(
                            outcome="PROMPT_NOT_REACHED",
                            wire_steps=wire_steps,
                            prefix_consumed=prefix_position,
                            expected_prefix_action=expected_action,
                            observed_action=next_action,
                        )
                    action_index = expected_index
                    prefix_position += 1
                    if next_action == "pick":
                        used_pick_indices.add(action_index)
                elif not target_reached:
                    if next_action != command:
                        return contextual(
                            outcome="PROMPT_NOT_REACHED",
                            wire_steps=wire_steps,
                            prefix_consumed=prefix_position,
                            expected_target_action=command,
                            observed_action=next_action,
                        )
                    target_reached = True
                    forced_target = True
                    action_index = int(index)
                    if command == "pick":
                        used_pick_indices.add(action_index)
                elif next_action == "pick":
                    action_index = _next_unused(used_pick_indices)
                    used_pick_indices.add(action_index)
                else:
                    action_index = 0

                if next_action == command:
                    same_action_occurrences += 1

            try:
                send_and_store(next_action, action_index=action_index)
            except BaseException as exc:
                failure = classify_probe_failure(
                    exc,
                    last_payload=(capture.last_payload if capture is not None else None),
                    action=next_action,
                )
                if failure.get("outcome") == "SEMANTIC_REJECTION" and not forced_target:
                    failure["outcome"] = "PROTOCOL_ERROR"
                outcome = contextual(
                    wire_steps=wire_steps,
                    same_action_occurrences=same_action_occurrences,
                    prefix_consumed=prefix_position,
                    **failure,
                )
                _persist_failure(artifact_dir, capture, outcome)
                return outcome

        return contextual(
            outcome="NONTERMINAL",
            wire_steps=wire_steps,
            prefix_consumed=prefix_position,
            error=f"continuation guard {_CONTINUATION_GUARD} reached",
        )
    except BaseException as exc:
        failure = classify_probe_failure(
            exc,
            last_payload=(capture.last_payload if capture is not None else None),
            action=command,
        )
        if failure.get("outcome") == "SEMANTIC_REJECTION":
            failure["outcome"] = "PROTOCOL_ERROR"
        outcome = contextual(**failure)
        _persist_failure(artifact_dir, capture, outcome)
        return outcome
    finally:
        try:
            if runtime is not None:
                runtime.session.close()
            else:
                raw_session.close()
        except Exception:
            pass


def _normalize_observed(
    observed: dict[Any, set[int]],
) -> list[tuple[str, str, tuple[str, ...], set[int]]]:
    merged: dict[tuple[str, str, tuple[str, ...]], set[int]] = {}
    for raw_key, raw_values in observed.items():
        if not isinstance(raw_key, tuple):
            continue
        if len(raw_key) == 2:
            parent_mode, action = raw_key
            prefix: tuple[str, ...] = ()
        elif len(raw_key) == 3:
            parent_mode, action, raw_prefix = raw_key
            if isinstance(raw_prefix, (list, tuple)):
                prefix = tuple(str(value) for value in raw_prefix)
            else:
                continue
        else:
            continue
        command = str(action or "").strip().lower()
        if command not in _INDEX_ACTIONS:
            continue
        values: set[int] = set()
        for raw in raw_values or set():
            if isinstance(raw, bool):
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                values.add(value)
        if not values:
            continue
        key = (str(parent_mode or "UNKNOWN"), command, prefix)
        merged.setdefault(key, set()).update(values)
    return [
        (parent, action, prefix, values)
        for (parent, action, prefix), values in sorted(
            merged.items(),
            key=lambda item: (
                item[0][0],
                item[0][1],
                len(item[0][2]),
                item[0][2],
            ),
        )
    ]


def expand_rubyplay_index_domains(
    provider,
    game: Game,
    result: GameTestResult,
    *,
    observed: dict[Any, set[int]],
    timeout_s: float,
    stop_event: threading.Event,
    progress,
    replay_fn: Callable[..., dict[str, Any]] = replay_index_probe,
    max_index: int = 32,
    prompt_retries: int = 8,
) -> GameTestResult:
    """Prove parent/path-scoped RubyPlay select/pick domains by live replay."""
    summaries: list[dict[str, Any]] = []
    run_root = Path(str(result.run_dir or "")) if result.run_dir else None
    try:
        prompt_budget = max(1, int(prompt_retries))
    except (TypeError, ValueError):
        prompt_budget = 8

    for parent_mode, action, prefix, observed_indices in _normalize_observed(observed):
        if stop_event.is_set():
            break
        prefix_label = _prefix_label(prefix)
        progress(
            f"[{game.name}] RubyPlay {parent_mode}/{action}/{prefix_label}: "
            "probando dominio de índices en sesiones frescas."
        )
        probe_counts: dict[int, int] = {}

        def probe(index: int) -> dict[str, Any]:
            prompt_attempts: list[dict[str, Any]] = []
            for _ in range(prompt_budget):
                attempt_no = probe_counts.get(index, 0) + 1
                probe_counts[index] = attempt_no
                artifact_dir = (
                    run_root
                    / "diagnostics"
                    / "choice-domain-probes"
                    / parent_mode
                    / action
                    / f"prefix-{prefix_label}"
                    / f"index-{index:03d}"
                    / f"attempt-{attempt_no:03d}"
                    if run_root is not None
                    else Path("diagnostics")
                    / "choice-domain-probes"
                    / parent_mode
                    / action
                    / f"prefix-{prefix_label}"
                    / f"index-{index:03d}"
                    / f"attempt-{attempt_no:03d}"
                )
                outcome = replay_fn(
                    provider,
                    game,
                    result,
                    parent_mode=parent_mode,
                    action=action,
                    prefix=prefix,
                    index=index,
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    artifact_dir=artifact_dir,
                )
                row = dict(outcome) if isinstance(outcome, dict) else {}
                row.setdefault("prefix", list(prefix))
                row.setdefault("artifact_dir", str(artifact_dir))
                prompt_attempts.append(row)
                if str(row.get("outcome") or "").upper() != "PROMPT_NOT_REACHED":
                    break
                if stop_event.is_set():
                    break

            final = dict(prompt_attempts[-1]) if prompt_attempts else {
                "index": index,
                "prefix": list(prefix),
                "outcome": "PROTOCOL_ERROR",
            }
            final["prompt_attempts"] = len(prompt_attempts)
            final["prompt_attempt_artifacts"] = [
                str(row.get("artifact_dir") or "")
                for row in prompt_attempts
                if str(row.get("artifact_dir") or "")
            ]
            return final

        proof = probe_contiguous_index_domain(probe, max_index=max_index)
        summary = {
            "parent_mode": parent_mode,
            "action": action,
            "prefix": list(prefix),
            "observed_indices": sorted(int(value) for value in observed_indices),
            "prompt_retry_budget": prompt_budget,
            **proof,
        }
        summaries.append(summary)

        if proof.get("state") != "PROVEN":
            continue

        mode_id = choice_domain_mode_id(parent_mode, action, prefix)
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
                "prefix": list(prefix),
                "observed": True,
                "executable": True,
                "wire_command": action,
                "observed_indices": sorted(
                    {int(value) for value in observed_indices}
                    | {int(value) for value in domain}
                ),
                "coverage_required": True,
                "branch_signature": choice_domain_signature(parent_mode, action, prefix),
                "required_options": domain,
                "covered_options": list(domain),
                "required_samples": 1,
                "sample_counts": {value: 1 for value in domain},
                "boundary_index": proof.get("boundary_index"),
                "boundary_confirmations": proof.get("boundary_confirmations"),
                "domain_authority": "isolated-live-server-boundary",
                "reason": (
                    "Every index below the boundary reached a clean terminal "
                    "fresh-session feature on the exact indexed prefix and the "
                    "next index was explicitly rejected twice by RubyPlay."
                ),
            }
        )
        progress(
            f"[{game.name}] RubyPlay {parent_mode}/{action}/{prefix_label}: "
            f"dominio demostrado={domain}, frontera={proof.get('boundary_index')}, "
            f"confirmaciones={proof.get('boundary_confirmations')}."
        )

    if run_root is not None:
        try:
            _write_json(
                run_root / "diagnostics" / "rubyplay-choice-domain-probes.json",
                {
                    "schema": "tester-spin/rubyplay-choice-domain-probes/v2",
                    "game": result.slug,
                    "domains": summaries,
                },
            )
        except OSError:
            pass
    return result


__all__ = [
    "choice_domain_mode_id",
    "choice_domain_signature",
    "expand_rubyplay_index_domains",
    "observed_index_prompts",
    "replay_index_probe",
]
