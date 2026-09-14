from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tester_spin.farm_contract import (
    SCHEMA,
    contains_forbidden_runtime_data,
    validate_common_contract,
)
from tester_spin.models import Game, GameTestResult


@dataclass(frozen=True, slots=True)
class ProviderFarmSpec:
    """Static provider boundary for exporting a finalized discovery result.

    This spec never executes protocol traffic. It only names stable protocol
    semantics that already exist in the provider implementation. Per-game wire
    selectors still come exclusively from ``GameTestResult.discovered_modes``.
    """

    provider: str
    protocol_family: str
    bootstrap_strategy: str
    transport: str
    terminal_contract: dict[str, Any]
    identifier_metadata_keys: tuple[str, ...] = ()
    stable_metadata_keys: tuple[str, ...] = ()
    mode_option_keys: tuple[str, ...] = ()
    runtime_outputs: tuple[str, ...] = ()
    protocol_static: dict[str, Any] = field(default_factory=dict)


_COMMON_MODE_KEYS = (
    "parent",
    "prefix",
    "states",
    "required_options",
    "covered_options",
    "required_samples",
    "sample_counts",
    "branch_signature",
)

_CONTINUATION_KINDS = {
    "CONTINUATION",
    "CHOICE_CONTINUATION",
    "FSO_BRANCH",
    "INDEXED_CHOICE",
}

_UNRESOLVED_KINDS = {
    "UNRESOLVED_STATE",
    "UNKNOWN",
    "UNKNOWN_FEATURE",
}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _stable_url(game: Game, metadata: dict[str, Any]) -> str:
    """Return a public/stable URL without captured query or fragment state."""

    candidates = (
        metadata.get("public_url"),
        metadata.get("page_url"),
        metadata.get("url"),
        game.url,
    )
    for raw in candidates:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            parsed = urlsplit(text)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return ""


def _stable_identifier(
    game: Game,
    result: GameTestResult,
    metadata: dict[str, Any],
    spec: ProviderFarmSpec,
) -> str:
    for value in (result.symbol, game.symbol):
        clean = str(value or "").strip()
        if clean:
            return clean
    for key in spec.identifier_metadata_keys:
        clean = str(metadata.get(key) or "").strip()
        if clean:
            return clean
    return ""


def _successful_terminal_attempt(result: GameTestResult, mode_id: str) -> bool:
    return any(
        str(attempt.mode_id or "") == mode_id
        and bool(attempt.ok)
        and bool(attempt.terminal)
        and not str(attempt.warning or "").strip()
        and not str(attempt.error or "").strip()
        for attempt in result.attempts
    )


def _coverage_complete(mode: dict[str, Any]) -> bool:
    required_raw = mode.get("required_options")
    if not isinstance(required_raw, list) or not required_raw:
        return False
    required = {str(value) for value in required_raw}
    if "DOMAIN_UNRESOLVED" in required:
        return False

    covered_raw = mode.get("covered_options")
    covered = (
        {str(value) for value in covered_raw}
        if isinstance(covered_raw, list)
        else set()
    )
    if not required.issubset(covered):
        return False

    try:
        required_samples = max(1, int(mode.get("required_samples") or 1))
    except (TypeError, ValueError):
        return False
    counts = mode.get("sample_counts")
    if isinstance(counts, dict):
        for value in required:
            try:
                if int(counts.get(value, 0)) < required_samples:
                    return False
            except (TypeError, ValueError):
                return False
    return True


def _mode_required(mode: dict[str, Any]) -> bool:
    kind = str(mode.get("kind") or "").upper()
    if bool(mode.get("coverage_required")):
        return True
    if kind in _UNRESOLVED_KINDS:
        return True
    if kind == "DISCOVERED_ONLY" and not bool(mode.get("executable")):
        return False
    return bool(mode.get("executable", True)) or kind in {
        "SPIN",
        "ANTE_BET",
        "PURCHASE",
        *_CONTINUATION_KINDS,
    }


def _mode_evidence(mode: dict[str, Any], result: GameTestResult) -> str:
    mode_id = str(mode.get("id") or "").strip()
    kind = str(mode.get("kind") or "").upper()

    if _successful_terminal_attempt(result, mode_id):
        return "DEMOSTRADO"

    if (
        str(mode.get("evidence_level") or "") == "REMOTE_EXECUTION"
        and str(mode.get("execution_state") or "") == "PROVEN_TERMINAL"
        and bool(mode.get("validated"))
    ):
        return "DEMOSTRADO"

    if bool(mode.get("coverage_required")) and _coverage_complete(mode):
        return "DEMOSTRADO"

    # Continuations are usually executed inside the root SpinAttempt and do not
    # receive their own attempt row. A finalized OK run proves an observed,
    # executable continuation only if no global coverage gate remained open.
    if (
        kind in _CONTINUATION_KINDS
        and result.status == "OK"
        and bool(mode.get("observed"))
        and bool(mode.get("executable", True))
        and not bool(mode.get("coverage_required"))
    ):
        return "DEMOSTRADO"

    execution_state = str(mode.get("execution_state") or "")
    evidence_level = str(mode.get("evidence_level") or "")
    if execution_state in {"ATTEMPTED_UNVALIDATED", "REJECTED_REMOTE"}:
        return "NO_VALIDADO"
    if execution_state == "NOT_ATTEMPTED" or bool(mode.get("executable")):
        return "CANDIDATO_WIRE"
    if evidence_level == "SERVER_ADVERTISED" or bool(mode.get("observed")):
        return "SOLO_ANUNCIADO"
    return "NO_VALIDADO"


def _copy_mode_options(mode: dict[str, Any], spec: ProviderFarmSpec) -> dict[str, Any]:
    options: dict[str, Any] = {}
    allowed = tuple(dict.fromkeys((*_COMMON_MODE_KEYS, *spec.mode_option_keys)))
    for key in allowed:
        if key not in mode:
            continue
        value = mode.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            options[key] = value
        elif isinstance(value, list):
            options[key] = list(value)
        elif isinstance(value, dict):
            options[key] = dict(value)
    return options


def _safe_metadata(
    metadata: dict[str, Any],
    spec: ProviderFarmSpec,
    unresolved: list[str],
) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for key in spec.stable_metadata_keys:
        if key not in metadata:
            continue
        value = metadata[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            selected[key] = value
        elif isinstance(value, list):
            selected[key] = list(value)
        elif isinstance(value, dict):
            selected[key] = dict(value)

    findings = contains_forbidden_runtime_data(selected, "$.protocol.stable_metadata")
    if not findings:
        return selected

    for finding in findings:
        reason = f"FORBIDDEN_RUNTIME_DATA:{finding}"
        if reason not in unresolved:
            unresolved.append(reason)

    safe: dict[str, Any] = {}
    for key, value in selected.items():
        if not contains_forbidden_runtime_data(
            {key: value},
            f"$.protocol.stable_metadata",
        ):
            safe[key] = value
    return safe


def build_result_farm_contract(
    game: Game,
    result: GameTestResult,
    game_dir: Path,
    spec: ProviderFarmSpec,
) -> dict[str, Any]:
    metadata = _read_json(Path(game_dir) / "game.json")
    unresolved: list[str] = []

    if result.status != "OK":
        unresolved.append(f"DISCOVERY_STATUS:{result.status}")

    identifier = _stable_identifier(game, result, metadata, spec)
    if not identifier:
        unresolved.append("IDENTIFIER_MISSING")

    public_url = _stable_url(game, metadata)
    if not public_url:
        unresolved.append("PUBLIC_GAME_URL_MISSING")

    modes: list[dict[str, Any]] = []
    known_continuations: list[str] = []
    unresolved_continuations: list[str] = []
    spin_demonstrated = False

    for raw_mode in result.discovered_modes:
        if not isinstance(raw_mode, dict):
            continue
        mode_id = str(raw_mode.get("id") or "").strip()
        if not mode_id:
            continue
        kind = str(raw_mode.get("kind") or "UNKNOWN").upper()
        command = str(raw_mode.get("wire_command") or "").strip()
        required = _mode_required(raw_mode)
        evidence = _mode_evidence(raw_mode, result)

        item = {
            "id": mode_id,
            "kind": kind,
            "required": required,
            "evidence": evidence,
            "executor": command or kind.lower(),
            "options": _copy_mode_options(raw_mode, spec),
        }
        modes.append(item)

        if mode_id == "SPIN" and evidence == "DEMOSTRADO":
            spin_demonstrated = True

        if kind in _CONTINUATION_KINDS:
            label = command or mode_id
            if evidence == "DEMOSTRADO":
                if label not in known_continuations:
                    known_continuations.append(label)
            elif required and label not in unresolved_continuations:
                unresolved_continuations.append(label)
        elif kind in _UNRESOLVED_KINDS:
            label = command or mode_id
            if label not in unresolved_continuations:
                unresolved_continuations.append(label)

        if required and evidence != "DEMOSTRADO":
            unresolved.append(f"MODE_NOT_DEMONSTRATED:{mode_id}:{evidence}")

    if not spin_demonstrated:
        unresolved.append("SPIN_CONTRACT_MISSING")

    stable_metadata = _safe_metadata(metadata, spec, unresolved)
    protocol = {
        "family": spec.protocol_family,
        "transport": spec.transport,
        "identifier": identifier,
        "stable_metadata": stable_metadata,
        "static": dict(spec.protocol_static),
        "modes": [dict(mode) for mode in modes],
    }

    contract: dict[str, Any] = {
        "schema": SCHEMA,
        "provider": spec.provider,
        "game": {
            "slug": result.slug or game.slug,
            "name": result.game_name or game.name,
            "symbol": identifier,
        },
        "ready": False,
        "source": {
            "run": result.finished_at,
            "result_status": result.status,
            "protocol_family": spec.protocol_family,
        },
        "bootstrap": {
            "strategy": spec.bootstrap_strategy,
            "inputs": {
                "public_game_url": public_url,
                "identifier": identifier,
            },
            "runtime_outputs": list(spec.runtime_outputs),
        },
        "modes": modes,
        "continuations": {
            "known": sorted(known_continuations),
            "unresolved": sorted(unresolved_continuations),
        },
        "terminal_contract": dict(spec.terminal_contract),
        "protocol": protocol,
        "unresolved": list(dict.fromkeys(unresolved)),
    }

    provider_errors = validate_result_farm_contract(contract, spec)
    semantic_errors = [
        error
        for error in provider_errors
        if error != "UNRESOLVED_ITEMS"
        and not error.startswith("REQUIRED_MODE_NOT_DEMONSTRATED:")
    ]
    for error in semantic_errors:
        if error not in contract["unresolved"]:
            contract["unresolved"].append(error)

    contract["ready"] = (
        result.status == "OK"
        and not contract["unresolved"]
        and not provider_errors
    )
    return contract


def validate_result_farm_contract(
    contract: dict[str, Any],
    spec: ProviderFarmSpec,
) -> list[str]:
    errors = list(validate_common_contract(contract))

    if contract.get("provider") != spec.provider:
        errors.append("PROVIDER_MISMATCH")

    source = contract.get("source")
    family = str(source.get("protocol_family") or "") if isinstance(source, dict) else ""
    if family != spec.protocol_family:
        errors.append("PROTOCOL_FAMILY_MISMATCH")

    bootstrap = contract.get("bootstrap")
    if not isinstance(bootstrap, dict):
        errors.append("BOOTSTRAP_MISSING")
    else:
        if bootstrap.get("strategy") != spec.bootstrap_strategy:
            errors.append("BOOTSTRAP_STRATEGY_MISMATCH")
        inputs = bootstrap.get("inputs")
        if not isinstance(inputs, dict):
            errors.append("BOOTSTRAP_INPUTS_MISSING")
        else:
            if not str(inputs.get("public_game_url") or "").strip():
                errors.append("PUBLIC_GAME_URL_MISSING")
            if not str(inputs.get("identifier") or "").strip():
                errors.append("IDENTIFIER_MISSING")

    protocol = contract.get("protocol")
    if not isinstance(protocol, dict):
        errors.append("PROTOCOL_MISSING")
    else:
        if protocol.get("family") != spec.protocol_family:
            errors.append("PROTOCOL_FAMILY_MISMATCH")
        if protocol.get("transport") != spec.transport:
            errors.append("PROTOCOL_TRANSPORT_MISMATCH")
        if not str(protocol.get("identifier") or "").strip():
            errors.append("IDENTIFIER_MISSING")

    modes = contract.get("modes")
    if not isinstance(modes, list) or not any(
        isinstance(mode, dict)
        and str(mode.get("id") or "") == "SPIN"
        and str(mode.get("evidence") or "") == "DEMOSTRADO"
        for mode in modes
    ):
        errors.append("SPIN_CONTRACT_MISSING")

    return list(dict.fromkeys(errors))
