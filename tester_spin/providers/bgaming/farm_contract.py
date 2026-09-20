from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tester_spin.farm_contract import (
    SCHEMA,
    contains_forbidden_runtime_data,
    validate_common_contract,
)
from tester_spin.models import Game, GameTestResult
from tester_spin.providers.bgaming.profile import (
    API_V2,
    HYPERHIVE,
    LEGACY_LINES,
    SWITCHABLE,
    UNKNOWN,
    BGamingProfile,
    profile_fingerprint,
)


_STRATEGIES = {
    API_V2: "bgaming-api-v2",
    LEGACY_LINES: "bgaming-legacy-lines",
    HYPERHIVE: "bgaming-hyperhive-jsonrpc",
    SWITCHABLE: "bgaming-switchable-container",
}

_PROFILE_MAPPING_FIELDS = (
    "spin_options",
    "command_options",
    "request_extra_data",
    "spin_option_choices",
    "effective_bet_multipliers",
)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _stable_public_url(game: Game, metadata: dict[str, Any]) -> str:
    public_url = str(metadata.get("public_url") or "").strip()
    if public_url:
        try:
            parsed = urlsplit(public_url)
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        except ValueError:
            return ""

    raw = str(game.url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _load_profile(metadata: dict[str, Any]) -> BGamingProfile | None:
    return BGamingProfile.from_dict(metadata.get("provider_protocol"))


_COVERAGE_OPTION_KEYS = (
    "scope",
    "parent",
    "prefix",
    "path_prefix",
    "option_field",
    "required_options",
    "covered_options",
    "required_samples",
    "sample_counts",
    "branch_signature",
    "source",
)


def _coverage_complete(mode: dict[str, Any]) -> bool:
    if mode.get("coverage_required") is not True:
        return False
    required_raw = mode.get("required_options")
    covered_raw = mode.get("covered_options")
    if not isinstance(required_raw, list) or not required_raw:
        return False
    if not isinstance(covered_raw, list):
        return False
    required = [str(value) for value in required_raw if str(value)]
    covered = {str(value) for value in covered_raw if str(value)}
    if not required or "DOMAIN_UNRESOLVED" in required:
        return False
    if any(value not in covered for value in required):
        return False
    try:
        samples = max(1, int(mode.get("required_samples", 1)))
    except (TypeError, ValueError):
        return False
    counts = mode.get("sample_counts")
    if samples > 1 and not isinstance(counts, dict):
        return False
    if isinstance(counts, dict):
        for value in required:
            try:
                count = max(0, int(counts.get(value, 0) or 0))
            except (TypeError, ValueError):
                return False
            if count < samples:
                return False
    return True


def _mode_evidence(mode: dict[str, Any], *, result_status: str) -> str:
    kind = str(mode.get("kind") or "").upper()
    evidence_level = str(mode.get("evidence_level") or "")
    execution_state = str(mode.get("execution_state") or "")

    if (
        evidence_level == "REMOTE_EXECUTION"
        and execution_state == "PROVEN_TERMINAL"
        and bool(mode.get("validated"))
    ):
        return "DEMOSTRADO"

    if _coverage_complete(mode):
        return "DEMOSTRADO"

    # Continuations are executed inside a root-mode attempt, so they do not get
    # their own SpinAttempt row. If the final discovery run is OK and the
    # continuation was actually observed, that root flow reached a validated
    # terminal state through this continuation.
    if kind == "CONTINUATION" and result_status == "OK" and bool(mode.get("observed")):
        return "DEMOSTRADO"

    if execution_state in {"ATTEMPTED_UNVALIDATED", "REJECTED_REMOTE"}:
        return "NO_VALIDADO"

    if evidence_level == "SERVER_ADVERTISED" or execution_state == "WIRE_UNPROVEN":
        return "SOLO_ANUNCIADO"

    if execution_state == "NOT_ATTEMPTED" or bool(mode.get("executable")):
        return "CANDIDATO_WIRE"

    return "SOLO_ANUNCIADO"


def _mode_proves_base_spin(mode: dict[str, Any], *, evidence: str) -> bool:
    """Return whether a demonstrated mode proves the mandatory base spin primitive.

    SPIN is protocol baseline, not an optional feature. A canonical SPIN mode
    proves it directly. Switchable containers prove the same primitive through
    their concrete lobby_switch+init+spin variant executor. Purchase commands
    that happen to be transported through "spin" must never satisfy the base
    spin requirement.
    """
    if evidence != "DEMOSTRADO":
        return False
    kind = str(mode.get("kind") or "").upper()
    command = str(mode.get("wire_command") or "").strip()
    if kind == "SPIN":
        if command == "spin":
            return True
        return (
            str(mode.get("wire_method") or "").strip() == "play"
            and mode.get("validated") is True
            and str(mode.get("execution_state") or "").strip() == "PROVEN_TERMINAL"
        )
    if kind == "VARIANT":
        return command == "lobby_switch+init+spin"
    return False

def _safe_profile_payload(
    profile: BGamingProfile,
    unresolved: list[str],
) -> dict[str, Any]:
    raw = {
        "capability_version": profile.capability_version,
        "spin_options": dict(profile.spin_options),
        "command_options": {
            str(command): dict(options)
            for command, options in profile.command_options.items()
            if isinstance(options, dict)
        },
        "request_extra_data": dict(profile.request_extra_data),
        "spin_option_choices": {
            str(name): list(values)
            for name, values in profile.spin_option_choices.items()
            if isinstance(values, list)
        },
        "effective_bet_selector": profile.effective_bet_selector,
        "effective_bet_multipliers": dict(profile.effective_bet_multipliers),
        "dynamic_purchased_feature": profile.dynamic_purchased_feature,
        "purchase_feature_level_supported": profile.purchase_feature_level_supported,
        "purchase_features": list(profile.purchase_features),
        "rows_required": profile.rows_required,
        "line_count": profile.line_count,
        "variable_layout": profile.variable_layout,
        "allowed_continuations": list(profile.allowed_continuations),
        "bundle_sha256": profile.bundle_sha256,
    }

    findings = contains_forbidden_runtime_data(raw, "$.protocol.profile")
    for finding in findings:
        reason = f"FORBIDDEN_RUNTIME_DATA:{finding}"
        if reason not in unresolved:
            unresolved.append(reason)

    if not findings:
        return raw

    # Never persist the suspicious runtime material, even in a non-ready
    # candidate. Drop only mapping-shaped runtime sections that contain a
    # forbidden key; static booleans/identifiers remain useful diagnostics.
    safe = dict(raw)
    for field in _PROFILE_MAPPING_FIELDS:
        value = safe.get(field)
        if contains_forbidden_runtime_data(value, f"$.protocol.profile.{field}"):
            safe[field] = {}
    return safe


def _mode_options(
    mode: dict[str, Any],
    profile: BGamingProfile | None,
) -> dict[str, Any]:
    command = str(mode.get("wire_command") or "").strip()
    options: dict[str, Any] = {}

    if profile is not None and command:
        if command == "spin":
            options.update(profile.spin_options)
        command_options = profile.command_options.get(command)
        if isinstance(command_options, dict):
            options.update(command_options)

    if str(mode.get("kind") or "").upper() == "PURCHASE":
        purchased_feature = str(mode.get("purchased_feature") or "").strip()
        if purchased_feature:
            options["purchased_feature"] = purchased_feature
        level = mode.get("purchased_feature_level")
        if level is not None:
            options["purchased_feature_level"] = level

    for key in _COVERAGE_OPTION_KEYS:
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


def build_bgaming_farm_contract(
    game: Game,
    result: GameTestResult,
    game_dir: Path,
) -> dict[str, Any]:
    metadata = _read_json(Path(game_dir) / "game.json")
    profile = _load_profile(metadata)
    unresolved: list[str] = []

    if result.status != "OK":
        unresolved.append(f"DISCOVERY_STATUS:{result.status}")

    family = profile.family if profile is not None else UNKNOWN
    if profile is None:
        unresolved.append("PROFILE_MISSING")
    else:
        if not profile.validated:
            unresolved.append("PROFILE_NOT_VALIDATED")
        if family == UNKNOWN or family not in _STRATEGIES:
            unresolved.append("PROTOCOL_FAMILY_UNKNOWN")

    identifier = str(result.symbol or game.symbol or metadata.get("identifier") or "").strip()
    if not identifier:
        unresolved.append("IDENTIFIER_MISSING")

    public_url = _stable_public_url(game, metadata)
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
        evidence = _mode_evidence(raw_mode, result_status=result.status)
        required = True

        item: dict[str, Any] = {
            "id": mode_id,
            "kind": kind,
            "required": required,
            "evidence": evidence,
            "executor": command or kind.lower(),
            "options": _mode_options(raw_mode, profile),
        }
        if isinstance(raw_mode.get("cost_multiplier"), (int, float)):
            item["cost_multiplier"] = raw_mode["cost_multiplier"]
        modes.append(item)

        if _mode_proves_base_spin(raw_mode, evidence=evidence):
            spin_demonstrated = True

        if kind == "CONTINUATION":
            if evidence == "DEMOSTRADO" and command:
                if command not in known_continuations:
                    known_continuations.append(command)
            else:
                label = command or mode_id
                if label not in unresolved_continuations:
                    unresolved_continuations.append(label)
        elif kind == "FEATURE" and evidence != "DEMOSTRADO":
            label = command or mode_id
            if label not in unresolved_continuations:
                unresolved_continuations.append(label)

        if evidence != "DEMOSTRADO":
            unresolved.append(f"MODE_NOT_DEMONSTRATED:{mode_id}:{evidence}")

    if not spin_demonstrated:
        unresolved.append("SPIN_CONTRACT_MISSING")

    protocol_profile: dict[str, Any] = {}
    client_fingerprint = ""
    if profile is not None:
        protocol_profile = _safe_profile_payload(profile, unresolved)
        client_fingerprint = profile_fingerprint(profile)

    protocol = {
        "family": family,
        "identifier": identifier,
        "profile": protocol_profile,
        "modes": [dict(mode) for mode in modes],
        "continuations": sorted(known_continuations),
    }

    contract: dict[str, Any] = {
        "schema": SCHEMA,
        "provider": "bgaming",
        "game": {
            "slug": result.slug or game.slug,
            "name": result.game_name or game.name,
            "symbol": identifier,
        },
        "ready": False,
        "source": {
            "run": result.finished_at,
            "result_status": result.status,
            "protocol_family": family,
            "client_fingerprint": client_fingerprint,
        },
        "bootstrap": {
            "strategy": _STRATEGIES.get(family, ""),
            "inputs": {
                "public_game_url": public_url,
                "identifier": identifier,
            },
            "runtime_outputs": [
                "session_endpoint",
                "csrf_header",
                "csrf_token",
                "round_series_id",
            ],
        },
        "modes": modes,
        "continuations": {
            "known": sorted(known_continuations),
            "unresolved": sorted(unresolved_continuations),
        },
        "terminal_contract": {
            "type": "provider",
            "name": family if family != UNKNOWN else "",
        },
        "protocol": protocol,
        "unresolved": list(dict.fromkeys(unresolved)),
    }

    # runtime_outputs are symbolic names of values created at bootstrap; they are
    # not persisted runtime values. Common secret scanning would interpret these
    # field names as secrets if stored as object keys, hence they remain strings.
    provider_errors = validate_bgaming_farm_contract(contract)
    semantic_errors = [
        error
        for error in provider_errors
        if error not in {"UNRESOLVED_ITEMS"}
        and not error.startswith("MODE_NOT_DEMONSTRATED:")
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


def validate_bgaming_farm_contract(contract: dict[str, Any]) -> list[str]:
    errors = list(validate_common_contract(contract))

    if contract.get("provider") != "bgaming":
        errors.append("INVALID_BGAMING_PROVIDER")

    source = contract.get("source")
    family = str(source.get("protocol_family") or "") if isinstance(source, dict) else ""
    if family not in _STRATEGIES:
        errors.append("PROTOCOL_FAMILY_UNKNOWN")

    bootstrap = contract.get("bootstrap")
    if not isinstance(bootstrap, dict):
        errors.append("BGAMING_BOOTSTRAP_MISSING")
    else:
        if bootstrap.get("strategy") != _STRATEGIES.get(family):
            errors.append("BGAMING_BOOTSTRAP_STRATEGY_MISMATCH")
        inputs = bootstrap.get("inputs")
        if not isinstance(inputs, dict):
            errors.append("BGAMING_BOOTSTRAP_INPUTS_MISSING")
        else:
            if not str(inputs.get("public_game_url") or "").strip():
                errors.append("PUBLIC_GAME_URL_MISSING")
            if not str(inputs.get("identifier") or "").strip():
                errors.append("IDENTIFIER_MISSING")

    protocol = contract.get("protocol")
    if not isinstance(protocol, dict):
        errors.append("BGAMING_PROTOCOL_MISSING")
    else:
        if str(protocol.get("family") or "") != family:
            errors.append("BGAMING_PROTOCOL_FAMILY_MISMATCH")
        if not str(protocol.get("identifier") or "").strip():
            errors.append("IDENTIFIER_MISSING")
        profile = protocol.get("profile")
        if not isinstance(profile, dict) or not profile:
            errors.append("BGAMING_PROFILE_MISSING")
        protocol_modes = protocol.get("modes")
        if not isinstance(protocol_modes, list) or not protocol_modes:
            errors.append("BGAMING_MODES_MISSING")

    unresolved = contract.get("unresolved")
    if isinstance(unresolved, list):
        for reason in unresolved:
            text = str(reason)
            if text.startswith("FORBIDDEN_RUNTIME_DATA:") and text not in errors:
                errors.append(text)

    return list(dict.fromkeys(errors))
