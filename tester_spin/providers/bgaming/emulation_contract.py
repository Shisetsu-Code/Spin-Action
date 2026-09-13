from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from tester_spin.models import Game, GameTestResult, utc_now_iso


CONTRACT_SCHEMA = "tester-spin/bgaming-emulation-contract/v1"
STATE_SCHEMA = "tester-spin/bgaming-state-machine/v1"
OUTCOME_SCHEMA = "tester-spin/bgaming-outcome-catalog/v1"
CONFORMANCE_SCHEMA = "tester-spin/bgaming-backend-conformance/v1"

_TRANSIENT_KEY_HINTS = {
    "round_series_id",
    "round_id",
    "last_action_id",
    "client_seed",
    "seed",
    "balance",
    "wallet",
    "win",
    "state_lock",
    "token",
    "play_token",
    "launch_token",
    "csrf",
    "id",
}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _safe_id(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    clean = clean.strip("_")
    return clean[:180] or "UNKNOWN"


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _merge_schema(values: list[Any]) -> dict[str, Any]:
    if not values:
        return {}
    types = sorted({_json_type(value) for value in values})
    schema: dict[str, Any] = {"type": types[0] if len(types) == 1 else types}

    dicts = [value for value in values if isinstance(value, dict)]
    if dicts:
        all_keys = sorted({str(key) for value in dicts for key in value})
        common_keys = set(all_keys)
        for value in dicts:
            common_keys &= {str(key) for key in value}
        properties: dict[str, Any] = {}
        for key in all_keys:
            children = [value[key] for value in dicts if key in value]
            properties[key] = _merge_schema(children)
        schema["properties"] = properties
        schema["required"] = sorted(common_keys)
        schema["additionalProperties"] = True

    lists = [value for value in values if isinstance(value, list)]
    if lists:
        items = [item for value in lists for item in value]
        schema["items"] = _merge_schema(items) if items else {}

    strings = [value for value in values if isinstance(value, str)]
    if strings and len(strings) == len(values):
        observed = sorted(set(strings))
        if 0 < len(observed) <= 12 and all(len(item) <= 120 for item in observed):
            schema["observed_enum"] = observed

    booleans = [value for value in values if isinstance(value, bool)]
    if booleans and len(booleans) == len(values):
        schema["observed_values"] = sorted(set(booleans))
    return schema


def _flatten_scalars(value: Any, prefix: str = "$") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(_flatten_scalars(item, f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value[:8]):
            out.update(_flatten_scalars(item, f"{prefix}[{index}]"))
    else:
        try:
            json.dumps(value)
        except TypeError:
            value = str(value)
        out[prefix] = value
    return out


def _dynamic_paths(samples: list[dict[str, Any]]) -> list[str]:
    by_path: dict[str, set[str]] = defaultdict(set)
    for sample in samples:
        for path, value in _flatten_scalars(sample).items():
            try:
                encoded = json.dumps(value, sort_keys=True, ensure_ascii=False)
            except TypeError:
                encoded = repr(value)
            by_path[path].add(encoded)

    dynamic: set[str] = {path for path, values in by_path.items() if len(values) > 1}
    for path in by_path:
        key = path.rsplit(".", 1)[-1].casefold()
        key = re.sub(r"\[\d+\]$", "", key)
        if any(hint in key for hint in _TRANSIENT_KEY_HINTS):
            dynamic.add(path)
    return sorted(dynamic)


def _request_variant(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    command = str(request.get("command") or "").strip()
    if command:
        options = request.get("options")
        options = options if isinstance(options, dict) else {}
        selectors = {
            str(key): value
            for key, value in options.items()
            if key not in {"bet", "bets"}
        }
        purchased = str(options.get("purchased_feature") or "").strip()
        level = options.get("purchased_feature_level")
        parts = [command.upper()]
        if purchased:
            parts.append("PURCHASE_" + purchased.upper())
        if level not in (None, ""):
            parts.append("LEVEL_" + str(level).upper())
        variant = "__".join(parts)
        return variant, {
            "transport": "HTTP_JSON",
            "wire_action": command,
            "selectors": selectors,
        }

    method = str(request.get("method") or "").strip()
    if method:
        params = request.get("params")
        params = params if isinstance(params, dict) else {}
        req = params.get("req")
        req = req if isinstance(req, dict) else {}
        purchased = str(req.get("purchased_feature") or "").strip()
        bonus_type = str(req.get("bonus_multiplier_type") or "").strip()
        action = str(req.get("action") or "").strip()
        parts = ["RPC_" + method.upper()]
        if purchased:
            parts.append("PURCHASE_" + purchased.upper())
        if bonus_type:
            parts.append(bonus_type.upper())
        if action:
            parts.append("ACTION_" + action.upper())
        selectors = {
            str(key): value
            for key, value in req.items()
            if key not in {"bet", "stake", "state_lock", "token"}
        }
        return "__".join(parts), {
            "transport": "HTTP_JSONRPC_2.0",
            "wire_action": method,
            "selectors": selectors,
        }

    return "UNKNOWN_ACTION", {
        "transport": "UNKNOWN",
        "wire_action": "",
        "selectors": {},
    }


def _api_response_state(response: dict[str, Any]) -> dict[str, Any] | None:
    flow = response.get("flow")
    if not isinstance(flow, dict):
        return None
    state = str(flow.get("state") or "").strip() or "unknown"
    actions = flow.get("available_actions")
    available = [str(item) for item in actions] if isinstance(actions, list) else []
    terminal = state == "closed" and "spin" in set(available)
    purchased = flow.get("purchased_feature")
    purchased_name = (
        str(purchased.get("name") or "")
        if isinstance(purchased, dict)
        else ""
    )
    return {
        "provider_state": state,
        "normalized_state": "READY" if terminal else f"FLOW:{state}",
        "terminal": terminal,
        "flow_command": str(flow.get("command") or ""),
        "available_actions": available,
        "purchased_feature": purchased_name,
        "round_id_present": flow.get("round_id") is not None,
    }


def _hyperhive_response_state(response: dict[str, Any]) -> dict[str, Any] | None:
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    final = result.get("final")
    if not isinstance(final, bool):
        return None
    next_action = result.get("nextAction")
    if next_action is None:
        next_action = result.get("next_action")
    available = [str(next_action)] if str(next_action or "").strip() else []
    return {
        "provider_state": "final" if final else "active",
        "normalized_state": "READY" if final else "ROUND_ACTIVE",
        "terminal": final,
        "flow_command": "play",
        "available_actions": available,
        "purchased_feature": "",
        "round_id_present": False,
    }


def _response_state(response: dict[str, Any]) -> dict[str, Any]:
    return (
        _api_response_state(response)
        or _hyperhive_response_state(response)
        or {
            "provider_state": "unknown",
            "normalized_state": "UNKNOWN",
            "terminal": False,
            "flow_command": "",
            "available_actions": [],
            "purchased_feature": "",
            "round_id_present": False,
        }
    )


def _outcome_kind(request: dict[str, Any], response: dict[str, Any]) -> tuple[str, str]:
    variant, request_meta = _request_variant(request)
    state = _response_state(response)
    action = str(request_meta.get("wire_action") or "").upper() or "UNKNOWN"
    selectors = request_meta.get("selectors")
    purchased = ""
    if isinstance(selectors, dict):
        purchased = str(selectors.get("purchased_feature") or "")
    if not purchased:
        purchased = str(state.get("purchased_feature") or "")

    if purchased:
        kind = "PURCHASE_RESULT"
    elif action in {"SPIN", "PLAY"}:
        kind = "BASE_OR_FEATURE_ENTRY"
    elif action in {"FREESPIN", "RESPIN", "PLAY_BONUS", "PRESELECTION_GAME", "CLOSE"}:
        kind = "CONTINUATION_RESULT"
    else:
        kind = "WIRE_RESULT"

    target = str(state.get("normalized_state") or "UNKNOWN")
    outcome_id = _safe_id(f"{variant}__TO__{target}").upper()
    return outcome_id, kind


def _nearest_init_state(path: Path, root: Path) -> str:
    current = path.parent
    while True:
        init_path = current / "init-response.json"
        if init_path.is_file():
            payload = _load_json(init_path)
            if payload:
                state = _response_state(payload)
                normalized = str(state.get("normalized_state") or "")
                if normalized and normalized != "UNKNOWN":
                    return normalized
                flow = payload.get("flow")
                if isinstance(flow, dict):
                    raw = str(flow.get("state") or "").strip()
                    if raw:
                        return "READY" if raw == "ready" else f"FLOW:{raw}"
                result = payload.get("result")
                if isinstance(result, dict):
                    return "READY"
        if current == root or current.parent == current:
            break
        try:
            current.relative_to(root)
        except ValueError:
            break
        current = current.parent
    return "READY"


def _attempt_pairs(root: Path) -> list[tuple[Path, dict[str, Any], dict[str, Any]]]:
    pairs: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for request_path in sorted(root.rglob("step-*-request.json")):
        response_path = Path(str(request_path).replace("-request.json", "-response.json"))
        if not response_path.is_file():
            continue
        request = _load_json(request_path)
        response = _load_json(response_path)
        if request is None or response is None:
            continue
        key = (str(request_path), str(response_path))
        if key not in seen:
            seen.add(key)
            pairs.append((request_path, request, response))

    # Some executors preserve only request.json/response.json. Add those only
    # when the same attempt has no step-001 pair.
    for request_path in sorted(root.rglob("request.json")):
        response_path = request_path.with_name("response.json")
        if not response_path.is_file():
            continue
        if (request_path.parent / "step-001-request.json").is_file():
            continue
        request = _load_json(request_path)
        response = _load_json(response_path)
        if request is None or response is None:
            continue
        key = (str(request_path), str(response_path))
        if key not in seen:
            seen.add(key)
            pairs.append((request_path, request, response))
    return pairs


def _profile_payloads(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(root.rglob("profile.json")):
        payload = _load_json(path)
        if payload is not None:
            out.append(payload)
    return out


def _mode_contracts(result: GameTestResult) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in result.discovered_modes:
        if not isinstance(item, dict):
            continue
        mode_id = str(item.get("id") or "").strip()
        if not mode_id:
            continue
        out.append(
            {
                "id": mode_id,
                "kind": str(item.get("kind") or ""),
                "wire_command": str(item.get("wire_command") or ""),
                "executable": bool(item.get("executable", True)),
                "coverage_required": bool(item.get("coverage_required", False)),
                "purchased_feature": item.get("purchased_feature"),
                "purchased_feature_level": item.get("purchased_feature_level"),
                "cost_multiplier": item.get("cost_multiplier"),
                "required_options": list(item.get("required_options") or []),
                "covered_options": list(item.get("covered_options") or []),
                "source": item.get("source"),
            }
        )
    return out


def generate_bgaming_emulation_contract(
    game: Game,
    result: GameTestResult,
) -> dict[str, Any]:
    """Build backend-facing BGaming wire/state artifacts from runtime evidence.

    The generated files deliberately do not infer RNG probabilities. They describe
    only request contracts, observed response families, and state transitions that
    Tester-Spin can support with persisted runtime/client evidence.
    """
    root = Path(str(result.run_dir or ""))
    if not root.is_dir():
        return {}

    pairs = _attempt_pairs(root)
    profiles = _profile_payloads(root)
    families = sorted(
        {
            str(profile.get("family") or "")
            for profile in profiles
            if str(profile.get("family") or "").strip()
        }
    )

    request_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    response_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    outcome_meta: dict[str, dict[str, Any]] = {}
    transition_counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
    transition_evidence: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    state_actions: dict[str, set[str]] = defaultdict(set)
    state_provider_names: dict[str, set[str]] = defaultdict(set)
    state_terminal: dict[str, bool] = defaultdict(bool)

    grouped: dict[Path, list[tuple[Path, dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for pair in pairs:
        grouped[pair[0].parent].append(pair)

    for attempt_dir, attempt_pairs in grouped.items():
        attempt_pairs.sort(key=lambda item: item[0].name)
        source_state = _nearest_init_state(attempt_dir, root)
        for request_path, request, response in attempt_pairs:
            variant, request_meta = _request_variant(request)
            outcome_id, outcome_kind = _outcome_kind(request, response)
            state = _response_state(response)
            target_state = str(state.get("normalized_state") or "UNKNOWN")
            action = str(request_meta.get("wire_action") or "UNKNOWN")
            evidence = str(request_path.relative_to(root)).replace("\\", "/")

            request_samples[variant].append(request)
            response_samples[outcome_id].append(response)
            meta = outcome_meta.setdefault(
                outcome_id,
                {
                    "id": outcome_id,
                    "kind": outcome_kind,
                    "request_contract": variant,
                    "source_states": set(),
                    "target_states": set(),
                    "provider_states": set(),
                    "available_actions": set(),
                    "observed_count": 0,
                    "evidence": [],
                    "terminal_observed": False,
                },
            )
            meta["source_states"].add(source_state)
            meta["target_states"].add(target_state)
            meta["provider_states"].add(str(state.get("provider_state") or "unknown"))
            meta["available_actions"].update(str(item) for item in state.get("available_actions") or [])
            meta["observed_count"] += 1
            meta["terminal_observed"] = bool(meta["terminal_observed"] or state.get("terminal"))
            if evidence not in meta["evidence"]:
                meta["evidence"].append(evidence)

            transition_key = (source_state, variant, target_state, outcome_id)
            transition_counts[transition_key] += 1
            if evidence not in transition_evidence[transition_key]:
                transition_evidence[transition_key].append(evidence)
            state_actions[source_state].add(action)
            state_provider_names[target_state].add(str(state.get("provider_state") or "unknown"))
            state_terminal[target_state] = bool(state_terminal[target_state] or state.get("terminal"))
            source_state = target_state

    schema_dir = root / "response-schemas"
    response_schema_refs: dict[str, str] = {}
    for outcome_id, samples in sorted(response_samples.items()):
        filename = _safe_id(outcome_id).lower() + ".json"
        rel = f"response-schemas/{filename}"
        response_schema_refs[outcome_id] = rel
        _write_json(
            schema_dir / filename,
            {
                "schema": "tester-spin/observed-json-shape/v1",
                "provider": "bgaming",
                "outcome_id": outcome_id,
                "authority": "RUNTIME_OBSERVED",
                "sample_count": len(samples),
                "json_schema": _merge_schema(samples),
                "dynamic_paths": _dynamic_paths(samples),
            },
        )

    request_contracts: list[dict[str, Any]] = []
    for contract_id, samples in sorted(request_samples.items()):
        _variant, meta = _request_variant(samples[0])
        request_contracts.append(
            {
                "id": contract_id,
                "transport": meta["transport"],
                "wire_action": meta["wire_action"],
                "selectors": meta["selectors"],
                "authority": "RUNTIME_OBSERVED",
                "sample_count": len(samples),
                "request_schema": _merge_schema(samples),
                "dynamic_paths": _dynamic_paths(samples),
            }
        )

    outcomes: list[dict[str, Any]] = []
    for outcome_id, raw in sorted(outcome_meta.items()):
        outcomes.append(
            {
                "id": outcome_id,
                "kind": raw["kind"],
                "request_contract": raw["request_contract"],
                "source_states": sorted(raw["source_states"]),
                "target_states": sorted(raw["target_states"]),
                "provider_states": sorted(raw["provider_states"]),
                "available_actions": sorted(raw["available_actions"]),
                "terminal_observed": bool(raw["terminal_observed"]),
                "observed_count": int(raw["observed_count"]),
                "response_schema": response_schema_refs.get(outcome_id, ""),
                "authority": "RUNTIME_OBSERVED",
                "wire_known": True,
                "state_transition_known": "UNKNOWN" not in raw["target_states"],
                "math_probability_known": False,
                "probability": None,
                "evidence": raw["evidence"],
            }
        )

    transitions: list[dict[str, Any]] = []
    for key, count in sorted(transition_counts.items()):
        source, request_contract, target, outcome_id = key
        transitions.append(
            {
                "source": source,
                "request_contract": request_contract,
                "outcome": outcome_id,
                "target": target,
                "observed_count": count,
                "authority": "RUNTIME_OBSERVED",
                "evidence": transition_evidence[key],
            }
        )

    states: dict[str, Any] = {}
    all_states = set(state_actions) | set(state_provider_names) | {item["target"] for item in transitions}
    for state_name in sorted(all_states):
        states[state_name] = {
            "provider_states": sorted(state_provider_names.get(state_name, set())),
            "allowed_wire_actions_observed": sorted(state_actions.get(state_name, set())),
            "terminal_observed": bool(state_terminal.get(state_name, False)),
        }

    dispatch: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for transition in transitions:
        bucket = dispatch[transition["source"]][transition["request_contract"]]
        if transition["outcome"] not in bucket:
            bucket.append(transition["outcome"])
    dispatch_payload = {
        state: {contract: sorted(values) for contract, values in sorted(actions.items())}
        for state, actions in sorted(dispatch.items())
    }

    unresolved = [
        item
        for item in _mode_contracts(result)
        if (
            (item["kind"] in {"SPIN", "PURCHASE", "FEATURE", "CONTINUATION", "CHOICE_BRANCH"})
            and (
                not item["executable"]
                or (
                    item["coverage_required"]
                    and set(map(str, item["required_options"]))
                    - set(map(str, item["covered_options"]))
                )
            )
        )
    ]

    common = {
        "provider": "bgaming",
        "game": {
            "slug": game.slug,
            "name": game.name,
            "identifier": result.symbol or game.symbol,
        },
        "generated_at": utc_now_iso(),
        "source_run": root.name,
        "runtime_families": families,
        "authority_order": [
            "RUNTIME_OBSERVED",
            "CLIENT_CONTRACT",
            "PROVIDER_METADATA",
            "INFERENCE",
        ],
    }

    protocol_contract = {
        "schema": CONTRACT_SCHEMA,
        **common,
        "status": result.status,
        "wire_replay_complete": bool(result.status == "OK" and pairs and not unresolved),
        "math_model_complete": False,
        "rng_probabilities_known": False,
        "request_contracts": request_contracts,
        "modes": _mode_contracts(result),
        "unresolved_modes": unresolved,
        "dispatch": dispatch_payload,
        "artifacts": {
            "state_machine": "state-machine.json",
            "outcome_catalog": "outcome-catalog.json",
            "backend_conformance": "backend-conformance.json",
            "response_schemas": "response-schemas/",
        },
    }

    state_machine = {
        "schema": STATE_SCHEMA,
        **common,
        "initial_state": "READY",
        "states": states,
        "transitions": transitions,
    }

    outcome_catalog = {
        "schema": OUTCOME_SCHEMA,
        **common,
        "note": (
            "Observed counts are protocol evidence only; they are not RNG weights or probabilities."
        ),
        "outcomes": outcomes,
    }

    conformance_cases = [
        {
            "id": f"CASE_{index:04d}",
            "source_state": transition["source"],
            "request_contract": transition["request_contract"],
            "accepted_outcome": transition["outcome"],
            "expected_target_state": transition["target"],
            "response_schema": response_schema_refs.get(transition["outcome"], ""),
            "authority": transition["authority"],
            "evidence": transition["evidence"],
        }
        for index, transition in enumerate(transitions, start=1)
    ]
    backend_conformance = {
        "schema": CONFORMANCE_SCHEMA,
        **common,
        "wire_replay_complete": protocol_contract["wire_replay_complete"],
        "cases": conformance_cases,
        "rules": [
            "A backend response must match the response schema for the selected outcome.",
            "The response must move the session to the documented target state.",
            "Every request contract valid in a source state must be dispatchable.",
            "Observed counts must never be interpreted as RNG probabilities.",
            "Unknown/unresolved modes must fail closed instead of inventing wire fields.",
        ],
    }

    _write_json(root / "protocol-contract.json", protocol_contract)
    _write_json(root / "state-machine.json", state_machine)
    _write_json(root / "outcome-catalog.json", outcome_catalog)
    _write_json(root / "backend-conformance.json", backend_conformance)
    _write_json(root / "emulation-contract.json", protocol_contract)
    return protocol_contract


__all__ = ["generate_bgaming_emulation_contract"]
