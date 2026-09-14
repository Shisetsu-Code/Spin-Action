from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit


SCHEMA = "tester-spin/farm-contract/v1"

_REQUIRED_TOP_LEVEL = (
    "schema",
    "provider",
    "game",
    "ready",
    "source",
    "bootstrap",
    "modes",
    "continuations",
    "terminal_contract",
    "protocol",
    "unresolved",
)

_FORBIDDEN_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "csrf",
    "csrf_token",
    "session",
    "session_id",
    "session_token",
    "launch_token",
    "play_token",
    "auth_token",
    "access_token",
    "refresh_token",
    "round_id",
    "roundid",
    "signature",
    "signed_url",
    "credentials",
    "password",
    "secret",
}

_FORBIDDEN_QUERY_KEYS = {
    "token",
    "launch_token",
    "play_token",
    "session",
    "session_id",
    "session_token",
    "auth",
    "authorization",
    "access_token",
    "refresh_token",
    "signature",
    "sig",
    "key",
    "api_key",
    "apikey",
}


def _normalized_key(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def contains_forbidden_runtime_data(value: Any, path: str = "$") -> list[str]:
    """Return JSON paths that contain persisted runtime secrets/ephemera.

    Keys are checked exactly after light normalization. String values are not
    treated as secrets merely because they contain words such as ``token``;
    however URLs are checked for sensitive query parameter names so a captured
    launch/session URL cannot be promoted accidentally.
    """

    findings: list[str] = []

    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if _normalized_key(key) in _FORBIDDEN_KEYS:
                findings.append(child_path)
                continue
            findings.extend(contains_forbidden_runtime_data(child, child_path))
        return findings

    if isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(
                contains_forbidden_runtime_data(child, f"{path}[{index}]")
            )
        return findings

    if isinstance(value, str) and "://" in value:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return findings
        for raw_key, _raw_value in parse_qsl(parsed.query, keep_blank_values=True):
            if _normalized_key(raw_key) in _FORBIDDEN_QUERY_KEYS:
                findings.append(f"{path}?{raw_key}")

    return findings


def validate_common_contract(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(contract, dict):
        return ["INVALID_CONTRACT"]

    for field in _REQUIRED_TOP_LEVEL:
        if field not in contract:
            errors.append(f"MISSING_FIELD:{field}")

    if contract.get("schema") != SCHEMA:
        errors.append("INVALID_SCHEMA")

    if not str(contract.get("provider") or "").strip():
        errors.append("INVALID_PROVIDER")

    game = contract.get("game")
    if not isinstance(game, dict):
        errors.append("INVALID_GAME")
    else:
        if not str(game.get("slug") or "").strip():
            errors.append("INVALID_GAME_SLUG")
        if not str(game.get("name") or "").strip():
            errors.append("INVALID_GAME_NAME")

    if not isinstance(contract.get("ready"), bool):
        errors.append("INVALID_READY")

    for field in ("source", "bootstrap", "continuations", "terminal_contract", "protocol"):
        if not isinstance(contract.get(field), dict):
            errors.append(f"INVALID_FIELD:{field}")

    modes = contract.get("modes")
    if not isinstance(modes, list):
        errors.append("INVALID_FIELD:modes")
        modes = []
    for index, mode in enumerate(modes):
        if not isinstance(mode, dict):
            errors.append(f"INVALID_MODE:{index}")
            continue
        mode_id = str(mode.get("id") or f"index-{index}")
        if bool(mode.get("required")) and str(mode.get("evidence") or "") != "DEMOSTRADO":
            errors.append(f"REQUIRED_MODE_NOT_DEMONSTRATED:{mode_id}")

    unresolved = contract.get("unresolved")
    if not isinstance(unresolved, list):
        errors.append("INVALID_FIELD:unresolved")
    elif unresolved:
        errors.append("UNRESOLVED_ITEMS")

    continuations = contract.get("continuations")
    if isinstance(continuations, dict):
        continuation_unresolved = continuations.get("unresolved")
        if continuation_unresolved not in (None, []) and not isinstance(
            continuation_unresolved, list
        ):
            errors.append("INVALID_CONTINUATIONS_UNRESOLVED")
        elif isinstance(continuation_unresolved, list) and continuation_unresolved:
            errors.append("UNRESOLVED_CONTINUATIONS")

    for forbidden_path in contains_forbidden_runtime_data(contract):
        errors.append(f"FORBIDDEN_RUNTIME_DATA:{forbidden_path}")

    # Keep diagnostics deterministic and avoid duplicated reasons from nested
    # provider/common validation merges.
    return list(dict.fromkeys(errors))


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def write_contract_candidate(game_dir: Path, contract: dict[str, Any]) -> Path:
    target = Path(game_dir) / "analysis" / "farm-contract-candidate.json"
    _atomic_write_json(target, contract)
    return target


def promote_contract_if_ready(game_dir: Path, contract: dict[str, Any]) -> bool:
    if not bool(contract.get("ready")):
        return False
    if validate_common_contract(contract):
        return False
    target = Path(game_dir) / "farm-contract.json"
    _atomic_write_json(target, contract)
    return True


def _merge_unresolved(contract: dict[str, Any], reasons: list[str]) -> None:
    existing = contract.get("unresolved")
    unresolved = [str(value) for value in existing] if isinstance(existing, list) else []
    for reason in reasons:
        value = str(reason or "").strip()
        # UNRESOLVED_ITEMS only summarizes that semantic reasons already exist;
        # persisting it would create noise and self-amplify on later validation.
        if not value or value == "UNRESOLVED_ITEMS":
            continue
        if value not in unresolved:
            unresolved.append(value)
    contract["unresolved"] = unresolved


def export_farm_contract(provider, game, result, *, progress) -> None:
    """Persist a provider-built contract candidate after final discovery gates.

    Export is deliberately best-effort: this artifact is for later headless farm
    execution and must never change the already-finalized protocol test status.
    """

    try:
        game_dir = provider.farm_contract_dir(game)
        if game_dir is None:
            return

        contract = provider.build_farm_contract(game, result)
        if not isinstance(contract, dict):
            raise TypeError("build_farm_contract debe devolver dict")

        provider_errors = provider.validate_farm_contract(contract)
        provider_errors = (
            [str(value) for value in provider_errors]
            if isinstance(provider_errors, list)
            else ["INVALID_PROVIDER_VALIDATION_RESULT"]
        )
        _merge_unresolved(contract, provider_errors)

        common_errors = validate_common_contract(contract)
        validation_errors = [
            error
            for error in common_errors
            if error not in {"UNRESOLVED_ITEMS"}
        ]
        _merge_unresolved(contract, validation_errors)

        if provider_errors or common_errors:
            contract["ready"] = False

        write_contract_candidate(Path(game_dir), contract)
        promoted = promote_contract_if_ready(Path(game_dir), contract)
        if promoted:
            progress("farm contract: PROMOTED")
            return

        unresolved = contract.get("unresolved")
        reasons = ", ".join(str(value) for value in unresolved or []) or "NOT_READY"
        progress(f"farm contract: candidate ready=false; unresolved={reasons}")
    except Exception as exc:
        progress(
            f"farm contract ERROR: {type(exc).__name__}: {exc}; "
            "el estado del protocolo no cambia."
        )
