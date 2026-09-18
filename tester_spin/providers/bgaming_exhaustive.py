from __future__ import annotations

import itertools
import hashlib
import math
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.bgaming import BGamingProvider as _BGamingProvider
from tester_spin.providers.bgaming import execution as _execution
from tester_spin.providers.bgaming.dynamic_index_domains import (
    PROTOCOL_ERROR as DYNAMIC_INDEX_PROTOCOL_ERROR,
    apply_index_domain_proof,
    probe_contiguous_index_domain,
    retry_until_target,
)
from tester_spin.providers.bgaming.flow_choices import (
    begin_flow_choice_run,
    begin_resolved_dynamic_choice_run,
    end_flow_choice_run,
    end_resolved_dynamic_choice_run,
    flow_choice_probe_result,
    install_flow_choice_adapter,
    register_resolved_dynamic_choice,
)


MAX_OPTION_COMBINATIONS = 128
MAX_FLOW_CHOICE_RUNS = 192
MAX_DYNAMIC_INDEX_PROBE_RUNS = 192

# The base executor owns profile discovery. Exhaustive replays only need a
# temporary, thread-local selector override; no per-game constants are stored.
_OVERRIDE_LOCAL = threading.local()
_ORIGINAL_DISCOVER_PROFILE = _execution.discover_profile


install_flow_choice_adapter()


def _same_option(left: Any, right: Any) -> bool:
    return (type(left) is type(right) and left == right) or str(left) == str(right)


def _apply_profile_override(profile):
    override = getattr(_OVERRIDE_LOCAL, "spin_options", None)
    if not isinstance(override, dict) or not override:
        return profile

    for field, selected in override.items():
        choices = profile.spin_option_choices.get(field)
        if not isinstance(choices, list) or not choices:
            raise ValueError(
                f"BGaming exhaustive: selector {field!r} sin dominio descubierto."
            )
        match = next(
            (value for value in choices if _same_option(value, selected)),
            None,
        )
        if match is None:
            raise ValueError(
                f"BGaming exhaustive: {field}={selected!r} no está en {choices!r}."
            )
        profile.spin_options[field] = match
        profile.evidence.append(f"exhaustive.override.{field}={match}")
    return profile


def _discover_profile_with_override(*args, **kwargs):
    return _apply_profile_override(_ORIGINAL_DISCOVER_PROFILE(*args, **kwargs))


if getattr(_execution.discover_profile, "__name__", "") != "_discover_profile_with_override":
    _execution.discover_profile = _discover_profile_with_override


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _safe_label(label: str) -> str:
    clean = [
        ch if ch.isalnum() or ch in {"-", "_", "."} else "_"
        for ch in str(label)
    ]
    digest = hashlib.sha256(str(label).encode("utf-8")).hexdigest()[:12]
    return ("".join(clean)[:150] or "BASE") + "-" + digest


def _load_profile(result: GameTestResult) -> dict[str, Any]:
    path = Path(str(result.run_dir or "")) / "profile.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _choice_domains(profile: dict[str, Any]) -> list[tuple[str, list[Any]]]:
    raw = profile.get("spin_option_choices")
    if not isinstance(raw, dict):
        return []

    domains: list[tuple[str, list[Any]]] = []
    for field in sorted(raw):
        # These form one purchase contract, already enumerated by the executor.
        # Crossing them with base spin or another feature fabricates invalid requests.
        if field in {"purchased_feature", "purchased_feature_level"}:
            continue
        values = raw.get(field)
        if not isinstance(values, list):
            continue
        clean: list[Any] = []
        for value in values:
            if value in (None, ""):
                continue
            if not any(_same_option(value, seen) for seen in clean):
                clean.append(value)
        if len(clean) > 1:
            domains.append((str(field), clean))
    return domains


def _matrix(domains: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:
    if not domains:
        return [{}]
    names = [name for name, _values in domains]
    return [
        dict(zip(names, values))
        for values in itertools.islice(
            itertools.product(*(values for _name, values in domains)),
            MAX_OPTION_COMBINATIONS + 1,
        )
    ]


def _label(combo: dict[str, Any]) -> str:
    if not combo:
        return "BASE"
    return "|".join(
        f"{field}={json.dumps(combo[field], ensure_ascii=False, sort_keys=True)}"
        for field in sorted(combo)
    )


def _base_combo(
    profile: dict[str, Any],
    domains: list[tuple[str, list[Any]]],
) -> dict[str, Any]:
    selected = profile.get("spin_options")
    if not isinstance(selected, dict):
        selected = {}

    combo: dict[str, Any] = {}
    for field, values in domains:
        current = selected.get(field)
        match = next(
            (value for value in values if _same_option(value, current)),
            None,
        )
        combo[field] = values[0] if match is None else match
    return combo


def _complete(result: GameTestResult) -> bool:
    return (
        result.status == "OK"
        and result.requested_spins > 0
        and result.successful_spins == result.requested_spins
        and result.failed_spins == 0
        and bool(result.attempts)
        and all(attempt.ok and attempt.terminal and not attempt.warning and not attempt.error for attempt in result.attempts)
    )


def _remap_attempt_paths(result: GameTestResult, old_root: Path, new_root: Path) -> None:
    for attempt in result.attempts:
        if not attempt.artifact_dir:
            continue
        path = Path(attempt.artifact_dir)
        try:
            rel = path.relative_to(old_root)
        except ValueError:
            continue
        attempt.artifact_dir = str(new_root / rel)


def _move_run(result: GameTestResult, target: Path) -> None:
    if not result.run_dir:
        return
    source = Path(str(result.run_dir or ""))
    if not source.is_dir():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"No se sobrescribe evidencia existente: {target}")
    if source.resolve() == target.resolve() or source.resolve() in target.resolve().parents:
        raise ValueError("El destino no puede estar dentro de la corrida de origen")
    shutil.move(str(source), str(target))
    _remap_attempt_paths(result, source, target)
    result.run_dir = str(target)


def _merge_modes(target: GameTestResult, source: GameTestResult) -> None:
    signatures = {
        (
            str(item.get("id") or ""),
            str(item.get("branch_signature") or ""),
        )
        for item in target.discovered_modes
        if isinstance(item, dict)
    }
    for item in source.discovered_modes:
        if not isinstance(item, dict):
            continue
        signature = (
            str(item.get("id") or ""),
            str(item.get("branch_signature") or ""),
        )
        if signature in signatures:
            continue
        target.discovered_modes.append(dict(item))
        signatures.add(signature)


def _trace_confirms(
    trace: list[dict[str, Any]],
    scope: str,
    command: str,
    path: tuple[str, ...],
) -> bool:
    return any(
        str(item.get("scope") or "") == scope
        and str(item.get("command") or item.get("wire_command") or "") == command
        and tuple(str(value) for value in item.get("path_after") or []) == path
        for item in trace
        if isinstance(item, dict)
    )


def _merge_choice_trace(
    graph: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]],
    trace: list[dict[str, Any]],
    *,
    complete: bool,
) -> None:
    for item in trace:
        if not isinstance(item, dict):
            continue
        scope = str(item.get("scope") or "SPIN")
        command = str(item.get("command") or item.get("wire_command") or "")
        prefix = tuple(str(value) for value in item.get("prefix") or [])
        available = tuple(
            str(value) for value in item.get("available") or [] if str(value)
        )
        if not command or not available:
            continue

        key = (scope, command, prefix)
        point = graph.setdefault(
            key,
            {
                "scope": scope,
                "command": command,
                "option_field": str(item.get("option_field") or ""),
                "source": str(item.get("source") or "runtime"),
                "prefix": prefix,
                "available": available,
                "covered": set(),
                "sample_counts": {},
                "unresolved_option_variants": [],
            },
        )
        merged_available = list(point.get("available") or ())
        for value in available:
            if value not in merged_available:
                merged_available.append(value)
        point["available"] = tuple(merged_available)
        unresolved_store = point.setdefault("unresolved_option_variants", [])
        for raw_variant in item.get("unresolved_option_variants") or []:
            if not isinstance(raw_variant, dict):
                continue
            normalized = {
                "literal_options": dict(raw_variant.get("literal_options") or {}),
                "unresolved_fields": [
                    str(field)
                    for field in raw_variant.get("unresolved_fields") or []
                    if str(field)
                ],
                "source": str(raw_variant.get("source") or ""),
            }
            marker = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
            existing = {
                json.dumps(value, ensure_ascii=False, sort_keys=True)
                for value in unresolved_store
                if isinstance(value, dict)
            }
            if marker not in existing:
                unresolved_store.append(normalized)
        selected = str(item.get("selected") or "")
        if complete and selected in available:
            point["covered"].add(selected)
            point["sample_counts"][selected] = point["sample_counts"].get(selected, 0) + 1


def _choice_mode_from_point(
    point: dict[str, Any],
    *,
    repetitions: int = 1,
) -> dict[str, Any]:
    scope = str(point.get("scope") or "SPIN")
    command = str(point.get("command") or "")
    prefix = tuple(str(value) for value in point.get("prefix") or ())
    required = [str(value) for value in point.get("available") or [] if str(value)]
    unresolved_variants = [
        dict(item)
        for item in point.get("unresolved_option_variants") or []
        if isinstance(item, dict)
    ]
    if unresolved_variants:
        required.append("DOMAIN_UNRESOLVED")
    try:
        samples = max(1, int(repetitions))
    except (TypeError, ValueError):
        samples = 1
    counts = point.get("sample_counts")
    if not isinstance(counts, dict):
        counts = {}

    def count(value: str) -> int:
        try:
            return max(0, int(counts.get(value, 0) or 0))
        except (TypeError, ValueError):
            return 0

    covered = [value for value in required if count(value) >= samples]
    mode_id = "BGAMING_FLOW_CHOICE_" + _safe_label(
        scope
        + "__"
        + command
        + "__"
        + "__".join(prefix or ("ROOT",))
    ).upper()
    return {
        "id": mode_id,
        "kind": "CHOICE_CONTINUATION",
        "scope": scope,
        "parent": scope,
        "observed": True,
        "executable": True,
        "wire_command": command,
        "option_field": str(point.get("option_field") or ""),
        "coverage_required": True,
        "branch_signature": (
            "BGAMING:flow-choice:"
            + scope
            + ":"
            + command
            + ":"
            + json.dumps(
                list(prefix),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        ),
        "prefix": list(prefix),
        "path_prefix": list(prefix),
        "required_options": required,
        "covered_options": covered,
        "required_samples": samples,
        "sample_counts": {
            value: count(value)
            for value in required
        },
        "unresolved_option_variants": unresolved_variants,
        "dynamic_index_proofs": [
            dict(item)
            for item in point.get("dynamic_index_proofs") or []
            if isinstance(item, dict)
        ],
        "dynamic_index_not_applicable": [
            dict(item)
            for item in point.get("dynamic_index_not_applicable") or []
            if isinstance(item, dict)
        ],
        "source": str(point.get("source") or "runtime"),
    }


def _dynamic_variant_identity(variant: dict[str, Any]) -> str:
    return json.dumps(
        {
            "literal_options": dict(variant.get("literal_options") or {}),
            "unresolved_fields": [
                str(value)
                for value in variant.get("unresolved_fields") or []
                if str(value)
            ],
            "source": str(variant.get("source") or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _confirmed_zero_semantic_rejection(
    proof: dict[str, Any],
    *,
    confirmations: int,
) -> bool:
    probes = [
        row
        for row in proof.get("probes") or []
        if isinstance(row, dict)
    ]
    if len(probes) < max(2, int(confirmations)):
        return False
    return all(
        int(row.get("index", -1)) == 0
        and str(row.get("outcome") or "").upper() == "SEMANTIC_REJECTION"
        for row in probes
    )


def _remove_unresolved_variant(
    point: dict[str, Any],
    variant: dict[str, Any],
) -> None:
    identity = _dynamic_variant_identity(variant)
    point["unresolved_option_variants"] = [
        item
        for item in point.get("unresolved_option_variants") or []
        if not (
            isinstance(item, dict)
            and _dynamic_variant_identity(item) == identity
        )
    ]


def _resolve_dynamic_index_domains(
    graph: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]],
    *,
    probe_value,
    register_option=None,
    max_index: int = 32,
    boundary_confirmations: int = 2,
) -> tuple[bool, list[dict[str, Any]]]:
    """Resolve client-proven single-index variants without guessing a domain."""
    changed = False
    proofs: list[dict[str, Any]] = []
    proven_contexts: dict[
        tuple[str, str, str],
        list[tuple[str, ...]],
    ] = {}

    ordered = sorted(
        graph.values(),
        key=lambda item: (
            -len(tuple(item.get("prefix") or ())),
            str(item.get("scope") or ""),
            str(item.get("command") or ""),
            tuple(str(value) for value in item.get("prefix") or ()),
        ),
    )
    for point in ordered:
        scope = str(point.get("scope") or "")
        command = str(point.get("command") or "")
        prefix = tuple(str(value) for value in point.get("prefix") or ())
        variants = [
            dict(item)
            for item in point.get("unresolved_option_variants") or []
            if isinstance(item, dict)
        ]
        for variant in variants:
            fields = [
                str(value)
                for value in variant.get("unresolved_fields") or []
                if str(value)
            ]
            if fields != ["index"]:
                continue

            identity = _dynamic_variant_identity(variant)
            context_key = (scope, command, identity)
            proof = probe_contiguous_index_domain(
                lambda index, p=point, v=variant: probe_value(p, v, index),
                max_index=max_index,
                boundary_confirmations=boundary_confirmations,
            )
            record = {
                **dict(proof),
                "scope": scope,
                "command": command,
                "prefix": list(prefix),
                "variant": dict(variant),
            }

            before_payloads = set(
                str(label)
                for label in (point.get("dynamic_option_payloads") or {})
            )
            applied = apply_index_domain_proof(point, variant, proof)
            if applied:
                changed = True
                proven_contexts.setdefault(context_key, []).append(prefix)
                if callable(register_option):
                    payloads = point.get("dynamic_option_payloads")
                    if isinstance(payloads, dict):
                        for label, payload in payloads.items():
                            if str(label) in before_payloads or not isinstance(payload, dict):
                                continue
                            register_option(
                                point,
                                str(label),
                                dict(payload),
                                "dynamic-index-boundary-proof",
                            )
                proofs.append(record)
                continue

            descendant_proven = any(
                len(proven_prefix) > len(prefix)
                and tuple(proven_prefix[: len(prefix)]) == prefix
                for proven_prefix in proven_contexts.get(context_key, [])
            )
            if (
                descendant_proven
                and _confirmed_zero_semantic_rejection(
                    proof,
                    confirmations=boundary_confirmations,
                )
            ):
                reason = (
                    "provider rejected index=0 at this prefix twice while the same "
                    "client variant had a proven finite domain at a descendant prefix"
                )
                _remove_unresolved_variant(point, variant)
                point.setdefault("dynamic_index_not_applicable", []).append(
                    {
                        "variant": dict(variant),
                        "prefix": list(prefix),
                        "reason": reason,
                        "proof": dict(proof),
                    }
                )
                record["not_applicable_at_prefix"] = True
                record["reason"] = reason
                changed = True

            proofs.append(record)

    return changed, proofs


def _resolve_dynamic_index_domains_until_stable(
    graph: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]],
    *,
    probe_value,
    replay_new_options,
    register_option=None,
    max_index: int = 32,
    boundary_confirmations: int = 2,
    max_passes: int = 8,
) -> tuple[bool, list[dict[str, Any]], int]:
    changed_any = False
    proofs: list[dict[str, Any]] = []
    try:
        guard = max(1, int(max_passes))
    except (TypeError, ValueError):
        guard = 8

    passes = 0
    for passes in range(1, guard + 1):
        changed, rows = _resolve_dynamic_index_domains(
            graph,
            probe_value=probe_value,
            register_option=register_option,
            max_index=max_index,
            boundary_confirmations=boundary_confirmations,
        )
        proofs.extend(rows)
        if not changed:
            break
        changed_any = True
        replay_new_options()
    return changed_any, proofs, passes


def _next_discovery_choice(
    graph: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]],
    attempted: set[tuple[str, str, tuple[str, ...]]],
) -> tuple[str, str, tuple[str, ...]] | None:
    """Return one structurally open choice branch at most once.

    Discovery runs are allowed to be non-terminal. Their purpose is to expose
    child prompts before finite domains are proven; exhaustive terminal retries
    happen only after dynamic domain resolution stabilizes.
    """
    ordered = sorted(
        graph.values(),
        key=lambda item: (
            str(item.get("scope") or ""),
            str(item.get("command") or ""),
            len(tuple(item.get("prefix") or ())),
            tuple(str(value) for value in item.get("prefix") or ()),
        ),
    )
    for point in ordered:
        counts = point.get("sample_counts")
        counts = counts if isinstance(counts, dict) else {}
        prefix = tuple(str(value) for value in point.get("prefix") or ())
        for raw_option in point.get("available") or ():
            option = str(raw_option)
            if not option or int(counts.get(option, 0) or 0) > 0:
                continue
            target = (
                str(point.get("scope") or ""),
                str(point.get("command") or ""),
                (*prefix, option),
            )
            if target not in attempted:
                return target
    return None


def _next_missing_choice(
    graph: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]],
    attempted: set[tuple[str, str, tuple[str, ...]]],
    repetitions: int = 1,
) -> tuple[str, str, tuple[str, ...]] | None:
    ordered = sorted(
        graph.values(),
        key=lambda item: (
            str(item["scope"]),
            str(item["command"]),
            len(item["prefix"]),
            tuple(item["prefix"]),
        ),
    )
    for point in ordered:
        for option in point["available"]:
            if point["sample_counts"].get(option, 0) >= repetitions:
                continue
            target = (
                str(point["scope"]),
                str(point["command"]),
                (*point["prefix"], str(option)),
            )
            if target not in attempted:
                return target
    return None


class BGamingProvider(_BGamingProvider):
    """BGaming with exhaustive traversal of client-proven finite choices."""

    def _raw_test(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
        forced_scope: str = "",
        forced_command: str = "",
        forced_path: tuple[str, ...] = (),
    ) -> tuple[GameTestResult, list[dict[str, Any]]]:
        from tester_spin.providers.bgaming.structural_map import reset_capture
        reset_capture()
        if forced_scope:
            _execution.set_execution_mode_filter(str(forced_scope))
        begin_flow_choice_run(
            forced_scope=forced_scope,
            forced_command=forced_command,
            forced_path=forced_path,
        )
        try:
            result = _BGamingProvider.test_game(
                self,
                game,
                spins=spins,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
            )
        finally:
            trace = end_flow_choice_run()
            if forced_scope:
                _execution.clear_execution_mode_filter()
        return result, trace

    def _raw_dynamic_index_probe(
        self,
        game: Game,
        *,
        scope: str,
        command: str,
        prefix: tuple[str, ...],
        variant: dict[str, Any],
        index: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> tuple[GameTestResult, list[dict[str, Any]], dict[str, Any]]:
        from tester_spin.providers.bgaming.structural_map import reset_capture

        reset_capture()
        literal_options = dict(variant.get("literal_options") or {})
        dynamic_probe = {
            "scope": str(scope),
            "command": str(command),
            "prefix": [str(value) for value in prefix],
            "literal_options": literal_options,
            "field": "index",
            "value": int(index),
        }
        _execution.set_execution_mode_filter(str(scope))
        begin_flow_choice_run(
            forced_scope=str(scope),
            forced_command=str(command),
            forced_path=tuple(str(value) for value in prefix),
            dynamic_probe=dynamic_probe,
        )
        probe: dict[str, Any] = {}
        try:
            result = _BGamingProvider.test_game(
                self,
                game,
                spins=1,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
            )
            probe = flow_choice_probe_result()
        finally:
            if not probe:
                probe = flow_choice_probe_result()
            trace = end_flow_choice_run()
            _execution.clear_execution_mode_filter()
        return result, trace, probe

    def _run_with_flow_choice_coverage(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        result, base_trace = self._raw_test(
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )
        if result.status in {"ERROR", "CANCELADO", "SIN_DEMO"} or not result.run_dir:
            return result
        if not base_trace:
            return result

        graph: dict[
            tuple[str, str, tuple[str, ...]],
            dict[str, Any],
        ] = {}
        _merge_choice_trace(graph, base_trace, complete=_complete(result))

        initial_root = Path(result.run_dir)
        master_root = initial_root.with_name(
            initial_root.name + f"-flow-choices-{time.time_ns() % 1_000_000_000:09d}"
        )
        _move_run(result, master_root)

        added_requested = 0
        added_successes = 0
        branch_errors: list[str] = []
        branch_diagnostics: list[str] = []
        attempted: set[tuple[str, str, tuple[str, ...]]] = set()
        discovery_attempted: set[tuple[str, str, tuple[str, ...]]] = set()
        executed = 0
        discovery_executed = 0

        dynamic_probe_runs = 0

        def _run_choice_target(
            target: tuple[str, str, tuple[str, ...]],
            *,
            discovery: bool,
        ) -> tuple[GameTestResult | None, list[dict[str, Any]], bool]:
            nonlocal added_requested, added_successes
            scope, command, forced_path = target
            path_label = " → ".join(forced_path)
            phase = "descubriendo" if discovery else "reproduciendo"
            progress(
                f"[{game.name}] BGaming {command}: {phase} "
                f"{scope} → {path_label}."
            )

            try:
                sub, trace = self._raw_test(
                    game,
                    spins=1 if discovery else max(1, int(spins)),
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=progress,
                    forced_scope=scope,
                    forced_command=command,
                    forced_path=forced_path,
                )
            except Exception as exc:
                branch_diagnostics.append(
                    f"{scope}/{command}/{path_label}: {type(exc).__name__}: {exc}"
                )
                return None, [], False

            target_dir = (
                master_root
                / (
                    "flow-choice-discovery-runs"
                    if discovery
                    else "flow-choice-runs"
                )
                / _safe_label(scope)
                / _safe_label(command)
                / _safe_label("__".join(forced_path))
            )
            if Path(str(sub.run_dir or "")).is_dir():
                _move_run(sub, target_dir)

            sub_complete = _complete(sub)
            _merge_choice_trace(graph, trace, complete=sub_complete)
            _merge_modes(result, sub)

            if not discovery:
                added_requested += sub.requested_spins
                added_successes += sub.successful_spins
                suffix = _safe_label(
                    scope + "__" + command + "__" + "__".join(forced_path)
                ).upper()
                for attempt in sub.attempts:
                    attempt.mode_id = f"{attempt.mode_id}__FLOW_{suffix}"
                    attempt.mode_kind = f"{attempt.mode_kind}_CHOICE_VARIANT"
                    result.attempts.append(attempt)

            if not _trace_confirms(trace, scope, command, forced_path):
                branch_diagnostics.append(
                    f"{scope}/{command}/{path_label}: "
                    "la ronda fresca no volvió a alcanzar esa rama"
                )
            elif not sub_complete:
                branch_diagnostics.append(
                    f"{scope}/{command}/{path_label}: "
                    f"{sub.status} {sub.error}".strip()
                )
            return sub, trace, sub_complete

        def discover_choice_graph() -> bool:
            nonlocal discovery_executed
            made_progress = False
            while not stop_event.is_set():
                target = _next_discovery_choice(graph, discovery_attempted)
                if target is None:
                    break
                discovery_attempted.add(target)
                if discovery_executed >= MAX_FLOW_CHOICE_RUNS:
                    branch_errors.append(
                        "descubrimiento de elecciones de flujo excede guard de "
                        f"{MAX_FLOW_CHOICE_RUNS} replays"
                    )
                    break
                discovery_executed += 1
                made_progress = True
                _run_choice_target(target, discovery=True)
            return made_progress

        def replay_missing_choices() -> bool:
            nonlocal executed
            made_progress = False
            while not stop_event.is_set():
                target = _next_missing_choice(
                    graph,
                    attempted,
                    max(1, int(spins)),
                )
                if target is None:
                    break
                attempted.add(target)
                if executed >= MAX_FLOW_CHOICE_RUNS:
                    branch_errors.append(
                        f"elecciones de flujo exceden guard de {MAX_FLOW_CHOICE_RUNS} replays"
                    )
                    break
                executed += 1
                made_progress = True
                _run_choice_target(target, discovery=False)
            return made_progress

        # First expose each branch once. An unresolved dynamic picker is allowed
        # to leave this discovery run non-terminal; retrying it before proving
        # its domain can never make the branch complete and only burns sessions.
        discover_choice_graph()

        def probe_dynamic_index(
            point: dict[str, Any],
            variant: dict[str, Any],
            index: int,
        ) -> dict[str, Any]:
            nonlocal dynamic_probe_runs
            scope = str(point.get("scope") or "")
            command = str(point.get("command") or "")
            prefix = tuple(str(value) for value in point.get("prefix") or ())
            path_label = "__".join(prefix) or "ROOT"

            progress(
                f"[{game.name}] BGaming {command}: probando índice {index} "
                f"en {scope} / {path_label}."
            )

            def run_once() -> dict[str, Any]:
                nonlocal dynamic_probe_runs
                dynamic_probe_runs += 1
                if dynamic_probe_runs > MAX_DYNAMIC_INDEX_PROBE_RUNS:
                    return {
                        "index": index,
                        "target_reached": False,
                        "outcome": DYNAMIC_INDEX_PROTOCOL_ERROR,
                        "error": (
                            "BGaming dynamic index probe guard exceeded "
                            f"({MAX_DYNAMIC_INDEX_PROBE_RUNS})"
                        ),
                    }
                try:
                    sub, _trace, probe = self._raw_dynamic_index_probe(
                        game,
                        scope=scope,
                        command=command,
                        prefix=prefix,
                        variant=variant,
                        index=index,
                        timeout_s=timeout_s,
                        stop_event=stop_event,
                        progress=progress,
                    )
                except Exception as exc:
                    return {
                        "index": index,
                        "target_reached": False,
                        "outcome": DYNAMIC_INDEX_PROTOCOL_ERROR,
                        "error": f"{type(exc).__name__}: {exc}",
                    }

                target_dir = (
                    master_root
                    / "dynamic-index-probes"
                    / _safe_label(scope)
                    / _safe_label(command)
                    / _safe_label(path_label)
                    / f"index-{int(index):03d}-sample-{dynamic_probe_runs:03d}"
                )
                if Path(str(sub.run_dir or "")).is_dir():
                    _move_run(sub, target_dir)

                observed = dict(probe) if isinstance(probe, dict) else {}
                observed["index"] = int(index)
                observed.setdefault("target_reached", False)
                _write_json(target_dir / "probe.json", observed)
                return observed

            observed = retry_until_target(run_once, max_attempts=6)
            observed["index"] = int(index)
            return observed

        def register_dynamic_option(
            point: dict[str, Any],
            label: str,
            payload: dict[str, Any],
            source: str,
        ) -> None:
            register_resolved_dynamic_choice(
                scope=str(point.get("scope") or ""),
                command=str(point.get("command") or ""),
                prefix=tuple(
                    str(value)
                    for value in point.get("prefix") or ()
                ),
                label=label,
                options=payload,
                source=source,
            )

        dynamic_changed, dynamic_proofs, dynamic_passes = (
            _resolve_dynamic_index_domains_until_stable(
                graph,
                probe_value=probe_dynamic_index,
                replay_new_options=discover_choice_graph,
                register_option=register_dynamic_option,
                max_index=32,
                boundary_confirmations=2,
                max_passes=8,
            )
        )
        if dynamic_proofs:
            _write_json(
                master_root / "dynamic-index-domain-proofs.json",
                {
                    "schema": "tester-spin/bgaming-dynamic-index-domains/v1",
                    "proofs": dynamic_proofs,
                    "probe_runs": dynamic_probe_runs,
                    "resolution_passes": dynamic_passes,
                    "changed": dynamic_changed,
                },
            )

        # Domains are now stable. Only at this point require terminal samples
        # for every literal and materialized indexed branch.
        if not stop_event.is_set():
            replay_missing_choices()

        result.requested_spins += added_requested
        result.successful_spins += added_successes
        result.failed_spins = max(
            0,
            result.requested_spins - result.successful_spins,
        )

        missing_labels: list[str] = []
        for point in sorted(
            graph.values(),
            key=lambda item: (
                str(item["scope"]),
                str(item["command"]),
                len(item["prefix"]),
                tuple(item["prefix"]),
            ),
        ):
            scope = str(point["scope"])
            command = str(point["command"])
            prefix = tuple(str(value) for value in point["prefix"])
            prefix_text = "ROOT" if not prefix else " → ".join(prefix)
            mode = _choice_mode_from_point(
                point,
                repetitions=max(1, int(spins)),
            )
            required = list(mode["required_options"])
            covered = list(mode["covered_options"])
            result.discovered_modes.append(mode)
            for value in required:
                if value not in covered:
                    missing_labels.append(
                        f"{scope}/{command}/{prefix_text} → {value}"
                    )

        if stop_event.is_set():
            result.status = "CANCELADO"
        elif (missing_labels or branch_errors) and result.status == "OK":
            result.status = "PARCIAL"
        if missing_labels or branch_errors:
            details: list[str] = []
            if missing_labels:
                details.append("faltan=" + ", ".join(missing_labels[:20]))
            if branch_errors:
                details.append("errores=" + " | ".join(branch_errors[:8]))
            message = (
                "BGaming cobertura de elecciones de flujo incompleta: "
                + "; ".join(details)
                + "."
            )
            if message not in str(result.error or ""):
                result.error = (
                    str(result.error or "").strip() + " " + message
                ).strip()

        _write_json(
            master_root / "flow-choice-coverage.json",
            {
                "schema": "tester-spin/bgaming-flow-choice-coverage/v1",
                "branch_points": [
                    {
                        "scope": str(point["scope"]),
                        "command": str(point["command"]),
                        "option_field": str(point.get("option_field") or ""),
                        "prefix": list(point["prefix"]),
                        "available": list(point["available"]),
                        "covered": [
                            value
                            for value in point["available"]
                            if value in point["covered"]
                        ],
                        "unresolved_option_variants": [
                            dict(item)
                            for item in point.get("unresolved_option_variants") or []
                            if isinstance(item, dict)
                        ],
                        "dynamic_index_proofs": [
                            dict(item)
                            for item in point.get("dynamic_index_proofs") or []
                            if isinstance(item, dict)
                        ],
                        "dynamic_index_not_applicable": [
                            dict(item)
                            for item in point.get("dynamic_index_not_applicable") or []
                            if isinstance(item, dict)
                        ],
                        "dynamic_option_payloads": {
                            str(label): dict(payload)
                            for label, payload in (
                                point.get("dynamic_option_payloads") or {}
                            ).items()
                            if str(label) and isinstance(payload, dict)
                        },
                        "source": str(point.get("source") or "runtime"),
                    }
                    for point in graph.values()
                ],
                "complete": not missing_labels and not branch_errors and not stop_event.is_set(),
                "replays": executed,
                "discovery_replays": discovery_executed,
                "replay_diagnostics": branch_diagnostics[-100:],
                "dynamic_index_probe_runs": dynamic_probe_runs,
            },
        )
        _write_json(master_root / "result.json", result.to_dict())
        return result

    def _test_game_exhaustive(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        result = self._run_with_flow_choice_coverage(
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )
        if result.status in {"ERROR", "CANCELADO", "SIN_DEMO"} or not result.run_dir:
            return result

        profile = _load_profile(result)
        domains = _choice_domains(profile)
        if not domains:
            return result

        combinations = _matrix(domains)
        total_combinations = math.prod(len(values) for _, values in domains)
        base_combo = _base_combo(profile, domains)
        base_label = _label(base_combo)
        required = [_label(combo) for combo in combinations]
        if total_combinations > len(combinations):
            required.append("MATRIX_LIMIT_EXCEEDED")
        covered: set[str] = {base_label} if _complete(result) else set()

        original_root = Path(result.run_dir)
        master_root = original_root.with_name(
            original_root.name + f"-exhaustive-{time.time_ns() % 1_000_000_000:09d}"
        )
        _move_run(result, master_root)

        added_requested = 0
        added_successes = 0
        branch_errors: list[str] = []
        executed = 1  # The original combination has already consumed a session.

        for combo in combinations:
            label = _label(combo)
            if label == base_label:
                continue
            if stop_event.is_set():
                branch_errors.append("detenido por el usuario")
                break
            if executed >= MAX_OPTION_COMBINATIONS:
                branch_errors.append(
                    f"matriz excede guard de {MAX_OPTION_COMBINATIONS} combinaciones"
                )
                break
            executed += 1
            progress(
                f"[{game.name}] BGaming opciones dinámicas: probando {label}."
            )
            _OVERRIDE_LOCAL.spin_options = dict(combo)
            try:
                sub = self._run_with_flow_choice_coverage(
                    game,
                    spins=max(1, int(spins)),
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=progress,
                )
            except Exception as exc:
                branch_errors.append(
                    f"{label}: {type(exc).__name__}: {exc}"
                )
                continue
            finally:
                _OVERRIDE_LOCAL.spin_options = None

            sub_root = Path(str(sub.run_dir or ""))
            target = master_root / "branch-runs" / _safe_label(label)
            if sub_root.is_dir():
                _move_run(sub, target)
            added_requested += sub.requested_spins
            added_successes += sub.successful_spins
            suffix = _safe_label(label).upper()
            for attempt in sub.attempts:
                attempt.mode_id = f"{attempt.mode_id}__OPTIONS_{suffix}"
                attempt.mode_kind = f"{attempt.mode_kind}_OPTION_VARIANT"
                result.attempts.append(attempt)
            _merge_modes(result, sub)
            if _complete(sub):
                covered.add(label)
            else:
                branch_errors.append(
                    f"{label}: {sub.status} {sub.error}".strip()
                )

        result.requested_spins += added_requested
        result.successful_spins += added_successes
        result.failed_spins = max(
            0,
            result.requested_spins - result.successful_spins,
        )
        result.discovered_modes.append(
            {
                "id": "BGAMING_SPIN_OPTION_MATRIX",
                "kind": "CHOICE_BRANCH",
                "observed": True,
                "executable": True,
                "wire_command": "additionalSpinOptions",
                "coverage_required": True,
                "branch_signature": "BGAMING:additionalSpinOptions:matrix",
                "dimensions": [
                    {"field": name, "values": values}
                    for name, values in domains
                ],
                "total_combinations": total_combinations,
                "required_options": required,
                "covered_options": sorted(covered),
            }
        )

        missing = [value for value in required if value not in covered]
        if stop_event.is_set():
            result.status = "CANCELADO"
        elif (missing or branch_errors) and result.status == "OK":
            result.status = "PARCIAL"
        if missing or branch_errors:
            details: list[str] = []
            if missing:
                details.append("faltan=" + ", ".join(missing[:20]))
            if branch_errors:
                details.append("errores=" + " | ".join(branch_errors[:8]))
            message = (
                "BGaming cobertura de additionalSpinOptions incompleta: "
                + "; ".join(details)
                + "."
            )
            if message not in str(result.error or ""):
                result.error = (
                    str(result.error or "").strip() + " " + message
                ).strip()

        _write_json(master_root / "result.json", result.to_dict())
        return result

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        begin_resolved_dynamic_choice_run()
        try:
            return self._test_game_exhaustive(
                game,
                spins=spins,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
            )
        finally:
            end_resolved_dynamic_choice_run()


# Preserve the public provider boundary used by registry/wiring tests.
BGamingProvider.__module__ = "tester_spin.providers.bgaming"

__all__ = ["BGamingProvider"]
