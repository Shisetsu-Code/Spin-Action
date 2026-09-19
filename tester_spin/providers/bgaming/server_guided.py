from __future__ import annotations

import hashlib
import json
import re
import threading
from typing import Any

from tester_spin.providers.bgaming.runtime import (
    BGamingRuntime,
    _collect_provider_script_contracts,
    _runtime_bundle_candidates,
    sanitize_session_url,
)


SCHEMA = "tester-spin/bgaming-server-guided-discovery/v1"
MAX_SEEDS = 96
MAX_ACTIONS = 24
MAX_CACHE_ENTRIES = 64

_GENERIC_KEYS = {
    "api_version", "balance", "command", "features", "flow", "game",
    "id", "name", "options", "outcome", "state", "type", "value",
}
_CACHE_LOCK = threading.Lock()
_CLIENT_CACHE: dict[tuple[str, str, str, str], tuple[str, str, str]] = {}
_DYNAMIC_LOCAL = threading.local()


def _safe_token(value: Any) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = str(value).strip()
    if not (2 <= len(text) <= 96):
        return ""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{1,95}", text):
        return ""
    return text


def _add_seed(out, seen, *, token: Any, path: str, kind: str) -> None:
    text = _safe_token(token)
    if not text or len(out) >= MAX_SEEDS:
        return
    key = (text, path)
    if key in seen:
        return
    seen.add(key)
    out.append({"token": text, "path": path, "kind": kind})


def server_search_seeds(data: dict[str, Any]) -> list[dict[str, str]]:
    """Extract bounded client-search terms from an authoritative server response."""
    if not isinstance(data, dict):
        return []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    flow = data.get("flow")
    if isinstance(flow, dict):
        _add_seed(out, seen, token=flow.get("state"), path="$.flow.state", kind="flow-state")
        _add_seed(out, seen, token=flow.get("command"), path="$.flow.command", kind="flow-command")
        actions = flow.get("available_actions")
        if isinstance(actions, list):
            for index, action in enumerate(actions[:MAX_ACTIONS]):
                _add_seed(out, seen, token=action, path=f"$.flow.available_actions[{index}]", kind="available-action")

    def walk(value: Any, path: str, depth: int) -> None:
        if depth > 5 or len(out) >= MAX_SEEDS:
            return
        if isinstance(value, dict):
            scalar_mapping = bool(value) and all(not isinstance(item, (dict, list)) for item in value.values())
            for raw_key, item in list(value.items())[:80]:
                key = str(raw_key)
                child = f"{path}.{key}"
                if key.casefold() not in _GENERIC_KEYS:
                    _add_seed(out, seen, token=key, path=child, kind="mapping-key" if scalar_mapping else "field")
                if isinstance(item, str):
                    _add_seed(out, seen, token=item, path=child, kind="enum-value")
                elif isinstance(item, (dict, list)):
                    walk(item, child, depth + 1)
        elif isinstance(value, list):
            for index, item in enumerate(value[:40]):
                child = f"{path}[{index}]"
                if isinstance(item, str):
                    _add_seed(out, seen, token=item, path=child, kind="enum-value")
                elif isinstance(item, (dict, list)):
                    walk(item, child, depth + 1)

    for root in ("features", "game", "options"):
        value = data.get(root)
        if isinstance(value, (dict, list)):
            walk(value, f"$.{root}", 0)
    return out


def _command_occurrences(bundle: str, action: str):
    if not action:
        return []
    pattern = re.compile(r"(?:[\"']?command[\"']?)\s*:\s*[\"']" + re.escape(action) + r"[\"']")
    return list(pattern.finditer(bundle or ""))[:32]


def _split_js_object_pairs(body: str) -> list[str]:
    pairs: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    depth = 0
    for char in body:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"\"", "'"}:
            quote = char
            current.append(char)
            continue
        if char in "([{":
            depth += 1
            current.append(char)
            continue
        if char in ")]}":
            depth = max(0, depth - 1)
            current.append(char)
            continue
        if char == "," and depth == 0:
            text = "".join(current).strip()
            if text:
                pairs.append(text)
            current = []
            continue
        current.append(char)
    text = "".join(current).strip()
    if text:
        pairs.append(text)
    return pairs


def _parse_js_literal(value: str) -> tuple[bool, Any]:
    raw = str(value or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"\"", "'"}:
        return True, raw[1:-1]
    lowered = raw.casefold()
    if lowered == "true":
        return True, True
    if lowered == "false":
        return True, False
    if lowered == "null":
        return True, None
    if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return True, float(raw) if "." in raw else int(raw)
    return False, None


def _parse_js_object_literal(body: str) -> tuple[dict[str, Any], list[str], list[str]]:
    literals: dict[str, Any] = {}
    unresolved: list[str] = []
    fields: list[str] = []
    for pair in _split_js_object_pairs(body):
        match = re.match(
            r"(?:[\"']?)([A-Za-z_][A-Za-z0-9_]*)(?:[\"']?)\s*:\s*(.+)$",
            pair,
        )
        if not match:
            continue
        field = str(match.group(1))
        if field not in fields:
            fields.append(field)
        ok, value = _parse_js_literal(match.group(2))
        if ok:
            literals[field] = value
        elif field not in unresolved:
            unresolved.append(field)
    return literals, unresolved, fields


def _variant_label(options: dict[str, Any]) -> str:
    if not options:
        return "__execute__"
    return "|".join(
        f"{key}={json.dumps(options[key], ensure_ascii=False, sort_keys=True)}"
        for key in sorted(options)
    )


def _nearest_forwarding_wrapper(bundle: str, position: int, forwarded: str) -> tuple[str, str] | None:
    start = max(0, position - 2200)
    left = (bundle or "")[start:position]
    patterns = (
        # Concise arrow wrappers forward options without a block body. Anchor
        # the request-object opening to this command occurrence so a preceding
        # unrelated arrow cannot establish execution authority.
        re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?\(?\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\)?\s*=>\s*[A-Za-z_$][A-Za-z0-9_$.]*\(\s*\{\s*$"),
        re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*async\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=>\s*\{"),
        re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*=>\s*\{"),
        re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*async\s*\(\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\)\s*=>\s*\{"),
        re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\(\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\)\s*\{"),
    )
    candidates: list[tuple[int, str, str]] = []
    for pattern in patterns:
        for match in pattern.finditer(left):
            method = str(match.group(1))
            parameter = str(match.group(2))
            if parameter == forwarded:
                candidates.append((match.end(), method, parameter))
    if not candidates:
        return None
    _end, method, parameter = max(candidates, key=lambda item: item[0])
    return method, parameter


def _forwarded_call_variants(
    bundle: str,
    method: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    executable: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    fields: list[str] = []
    pattern = re.compile(
        r"(?:this\.)?" + re.escape(method) + r"\(\s*\{([^{}]{0,600})\}\s*\)"
    )
    seen_exec: set[str] = set()
    seen_unresolved: set[str] = set()
    for match in pattern.finditer(bundle or ""):
        literal_options, unresolved_fields, option_fields = _parse_js_object_literal(
            match.group(1)
        )
        for field in option_fields:
            if field not in fields:
                fields.append(field)
        if unresolved_fields:
            key = json.dumps(
                {
                    "options": literal_options,
                    "unresolved_fields": sorted(unresolved_fields),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if key not in seen_unresolved:
                seen_unresolved.add(key)
                unresolved.append(
                    {
                        "literal_options": dict(literal_options),
                        "unresolved_fields": list(unresolved_fields),
                        "source": f"client-callsite:{method}",
                    }
                )
            continue
        label = _variant_label(literal_options)
        if label in seen_exec:
            continue
        seen_exec.add(label)
        executable.append(
            {
                "label": label,
                "options": dict(literal_options),
                "source": f"client-callsite:{method}",
            }
        )
    return executable, unresolved, fields


def _serializer_details(bundle: str, occurrence) -> dict[str, Any]:
    window = (bundle or "")[occurrence.end(): occurrence.end() + 700]
    direct = re.search(
        r"(?:[\"']?options[\"']?)\s*:\s*\{([^{}]{0,500})\}",
        window,
    )
    if direct:
        literal_options, unresolved_fields, option_fields = _parse_js_object_literal(
            direct.group(1)
        )
        parameterless = not option_fields and not direct.group(1).strip()
        executable: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        if parameterless:
            executable.append(
                {
                    "label": "__execute__",
                    "options": {},
                    "source": "client-inline-options",
                }
            )
        elif unresolved_fields:
            unresolved.append(
                {
                    "literal_options": dict(literal_options),
                    "unresolved_fields": list(unresolved_fields),
                    "source": "client-inline-options",
                }
            )
        else:
            executable.append(
                {
                    "label": _variant_label(literal_options),
                    "options": dict(literal_options),
                    "source": "client-inline-options",
                }
            )
        return {
            "shape_proven": True,
            "parameterless": parameterless,
            "option_fields": option_fields,
            "option_variants": executable,
            "unresolved_option_variants": unresolved,
            "forwarded_options_parameter": "",
            "client_wrapper": "",
        }

    forwarded = re.search(
        r"(?:[\"']?options[\"']?)\s*:\s*([A-Za-z_$][A-Za-z0-9_$]*)",
        window,
    )
    if forwarded:
        variable = str(forwarded.group(1))
        wrapper = _nearest_forwarding_wrapper(bundle, occurrence.start(), variable)
        if wrapper is None:
            return {
                "shape_proven": True,
                "parameterless": False,
                "option_fields": [],
                "option_variants": [],
                "unresolved_option_variants": [],
                "forwarded_options_parameter": variable,
                "client_wrapper": "",
            }
        method, _parameter = wrapper
        executable, unresolved, option_fields = _forwarded_call_variants(
            bundle,
            method,
        )
        return {
            "shape_proven": True,
            "parameterless": False,
            "option_fields": option_fields,
            "option_variants": executable,
            "unresolved_option_variants": unresolved,
            "forwarded_options_parameter": variable,
            "client_wrapper": method,
        }

    return {
        "shape_proven": False,
        "parameterless": False,
        "option_fields": [],
        "option_variants": [],
        "unresolved_option_variants": [],
        "forwarded_options_parameter": "",
        "client_wrapper": "",
    }


def _merge_unique_variant(target: list[dict[str, Any]], item: dict[str, Any]) -> None:
    marker = json.dumps(item, ensure_ascii=False, sort_keys=True)
    existing = {
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        for value in target
        if isinstance(value, dict)
    }
    if marker not in existing:
        target.append(dict(item))


def analyze_server_response_against_bundle(data: dict[str, Any], bundle: str, *, source: str = "") -> dict[str, Any]:
    """Correlate server-advertised actions with provider-client wire evidence."""
    seeds = server_search_seeds(data)
    flow = data.get("flow") if isinstance(data, dict) else None
    flow = flow if isinstance(flow, dict) else {}
    raw_actions = flow.get("available_actions")
    actions: list[str] = []
    if isinstance(raw_actions, list):
        for raw in raw_actions:
            action = _safe_token(raw)
            if action and action not in actions:
                actions.append(action)
            if len(actions) >= MAX_ACTIONS:
                break

    rows = []
    windows: dict[str, list[str]] = {}
    for action in actions:
        occurrences = _command_occurrences(bundle, action)
        fields: list[str] = []
        option_variants: list[dict[str, Any]] = []
        unresolved_variants: list[dict[str, Any]] = []
        parameterless = False
        shape_proven = False
        wrappers: list[str] = []
        forwarded_parameters: list[str] = []
        windows[action] = []
        for occurrence in occurrences:
            details = _serializer_details(bundle, occurrence)
            shape_proven = shape_proven or bool(details.get("shape_proven"))
            parameterless = parameterless or bool(details.get("parameterless"))
            for field in details.get("option_fields") or []:
                text = str(field)
                if text and text not in fields:
                    fields.append(text)
            for variant in details.get("option_variants") or []:
                if isinstance(variant, dict):
                    _merge_unique_variant(option_variants, variant)
            for variant in details.get("unresolved_option_variants") or []:
                if isinstance(variant, dict):
                    _merge_unique_variant(unresolved_variants, variant)
            wrapper = str(details.get("client_wrapper") or "")
            if wrapper and wrapper not in wrappers:
                wrappers.append(wrapper)
            forwarded = str(details.get("forwarded_options_parameter") or "")
            if forwarded and forwarded not in forwarded_parameters:
                forwarded_parameters.append(forwarded)
            windows[action].append((bundle or "")[max(0, occurrence.start() - 1200): occurrence.start() + 1800])
        replay_eligible = bool(shape_proven and (parameterless or option_variants))
        rows.append({
            "action": action,
            "advertised": True,
            "client_command_literal_hits": len(occurrences),
            "option_fields": fields,
            "parameterless": parameterless,
            "option_variants": option_variants,
            "unresolved_option_variants": unresolved_variants,
            "client_wrappers": wrappers,
            "forwarded_options_parameters": forwarded_parameters,
            "serializer_shape_proven": bool(occurrences and shape_proven),
            "replay_eligible": replay_eligible,
            "execution_authority": "evidence-only",
        })

    seed_rows = []
    for seed in seeds:
        token = seed["token"]
        seed_rows.append({
            **seed,
            "client_hits": min((bundle or "").count(token), 999),
            "near_actions": [action for action, values in windows.items() if token == action or any(token in value for value in values)],
        })

    return {
        "schema": SCHEMA,
        "flow": {
            "state": str(flow.get("state") or ""),
            "command": str(flow.get("command") or ""),
            "available_actions": actions,
        },
        "client": {
            "source": sanitize_session_url(source) if source else "",
            "bundle_sha256": hashlib.sha256((bundle or "").encode("utf-8", errors="replace")).hexdigest() if bundle else "",
        },
        "actions": rows,
        "search_seeds": seed_rows,
    }


def begin_dynamic_contract_run() -> None:
    _DYNAMIC_LOCAL.specs = {}


def end_dynamic_contract_run() -> None:
    _DYNAMIC_LOCAL.specs = {}


def _dynamic_specs() -> dict[str, dict[str, Any]]:
    specs = getattr(_DYNAMIC_LOCAL, "specs", None)
    if not isinstance(specs, dict):
        specs = {}
        _DYNAMIC_LOCAL.specs = specs
    return specs


def remember_dynamic_evidence(evidence: dict[str, Any]) -> None:
    """Remember only fully client-proven replay payloads for this worker thread."""
    if not isinstance(evidence, dict):
        return
    flow = evidence.get("flow")
    state = str(flow.get("state") or "") if isinstance(flow, dict) else ""
    client = evidence.get("client")
    source = str(client.get("source") or "") if isinstance(client, dict) else ""
    actions = evidence.get("actions")
    if not isinstance(actions, list):
        return
    specs = _dynamic_specs()
    for raw in actions:
        if not isinstance(raw, dict):
            continue
        action = str(raw.get("action") or "")
        if not action or action in {"init", "spin"}:
            continue
        variants = raw.get("option_variants")
        variants = variants if isinstance(variants, list) else []
        unresolved_variants = raw.get("unresolved_option_variants")
        unresolved_variants = (
            unresolved_variants if isinstance(unresolved_variants, list) else []
        )
        if not variants and not unresolved_variants:
            continue
        spec = specs.setdefault(
            action,
            {
                "states": set(),
                "variants": {},
                "unresolved_variants": [],
                "source": source,
                "option_fields": [],
            },
        )
        if state:
            spec["states"].add(state)
        if source and not spec.get("source"):
            spec["source"] = source
        for field in raw.get("option_fields") or []:
            text = str(field)
            if text and text not in spec["option_fields"]:
                spec["option_fields"].append(text)
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            label = str(variant.get("label") or "")
            options = variant.get("options")
            if label and isinstance(options, dict):
                spec["variants"][label] = dict(options)
        unresolved_store = spec.setdefault("unresolved_variants", [])
        for variant in unresolved_variants:
            if not isinstance(variant, dict):
                continue
            normalized = {
                "literal_options": dict(variant.get("literal_options") or {}),
                "unresolved_fields": [
                    str(field)
                    for field in variant.get("unresolved_fields") or []
                    if str(field)
                ],
                "source": str(variant.get("source") or ""),
            }
            marker = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
            existing = {
                json.dumps(item, ensure_ascii=False, sort_keys=True)
                for item in unresolved_store
                if isinstance(item, dict)
            }
            if marker not in existing:
                unresolved_store.append(normalized)


def dynamic_action_variants(data: dict[str, Any], action: str) -> list[dict[str, Any]]:
    """Return replay-safe client-proven variants for an advertised action."""
    if not isinstance(data, dict):
        return []
    flow = data.get("flow")
    if not isinstance(flow, dict):
        return []
    actions = flow.get("available_actions")
    advertised = {str(item) for item in actions} if isinstance(actions, list) else set()
    command = str(action or "")
    if command not in advertised:
        return []
    spec = _dynamic_specs().get(command)
    if not isinstance(spec, dict):
        return []
    state = str(flow.get("state") or "")
    states = spec.get("states")
    if isinstance(states, set) and states and state not in states:
        return []
    variants = spec.get("variants")
    if not isinstance(variants, dict):
        return []
    return [
        {"label": str(label), "options": dict(options)}
        for label, options in variants.items()
        if str(label) and isinstance(options, dict)
    ]


def dynamic_action_unresolved_variants(
    data: dict[str, Any],
    action: str,
) -> list[dict[str, Any]]:
    """Return unresolved client variants only in their observed runtime state."""
    if not isinstance(data, dict):
        return []
    flow = data.get("flow")
    if not isinstance(flow, dict):
        return []
    command = str(action or "")
    actions = flow.get("available_actions")
    advertised = {str(item) for item in actions} if isinstance(actions, list) else set()
    if command not in advertised:
        return []

    spec = _dynamic_specs().get(command)
    if not isinstance(spec, dict):
        return []
    state = str(flow.get("state") or "")
    states = spec.get("states")
    if isinstance(states, set) and states and state not in states:
        return []

    rows = spec.get("unresolved_variants")
    if not isinstance(rows, list):
        return []
    return [dict(item) for item in rows if isinstance(item, dict)]


def dynamic_action_variants_any_state(action: str) -> list[dict[str, Any]]:
    """Return client-proven literal variants without requiring prior state scan.

    Callers must provide an independent runtime authority before using this
    fallback. This is intentionally not used for ordinary replay discovery.
    """
    spec = _dynamic_specs().get(str(action or ""))
    variants = spec.get("variants") if isinstance(spec, dict) else None
    if not isinstance(variants, dict):
        return []
    return [
        {"label": str(label), "options": dict(options)}
        for label, options in variants.items()
        if str(label) and isinstance(options, dict)
    ]


def dynamic_action_unresolved_variants_any_state(
    action: str,
) -> list[dict[str, Any]]:
    """Return client-proven unresolved variants independent of observed state."""
    spec = _dynamic_specs().get(str(action or ""))
    rows = spec.get("unresolved_variants") if isinstance(spec, dict) else None
    if not isinstance(rows, list):
        return []
    return [dict(item) for item in rows if isinstance(item, dict)]


def dynamic_action_source(action: str) -> str:
    spec = _dynamic_specs().get(str(action or ""))
    if not isinstance(spec, dict):
        return "server-guided-client"
    source = str(spec.get("source") or "")
    return f"server-guided-client:{source}" if source else "server-guided-client"


def dynamic_action_option_fields(action: str) -> list[str]:
    spec = _dynamic_specs().get(str(action or ""))
    fields = spec.get("option_fields") if isinstance(spec, dict) else None
    return [str(item) for item in fields if str(item)] if isinstance(fields, list) else []


def _cache_key(runtime: BGamingRuntime) -> tuple[str, str, str, str]:
    return (
        str(runtime.identifier or ""),
        str(runtime.options.get("resources_path") or ""),
        str(runtime.options.get("game_bundle_source") or ""),
        str(runtime.options.get("games_loader_source") or ""),
    )


def _load_client_bundle(runtime: BGamingRuntime, *, timeout_s: float) -> tuple[str, str]:
    key = _cache_key(runtime)
    with _CACHE_LOCK:
        cached = _CLIENT_CACHE.get(key)
    if cached is not None:
        return cached[0], cached[1]

    diagnostics: list[dict[str, Any]] = []
    seeds = _runtime_bundle_candidates(runtime, timeout_s=timeout_s, diagnostics=diagnostics)
    parts = _collect_provider_script_contracts(runtime, timeout_s=timeout_s, seeds=seeds, diagnostics=diagnostics)
    parts.sort(key=lambda item: item[0], reverse=True)
    bundle = "\n".join(item[2] for item in parts)
    source = parts[0][1] if parts else ""
    digest = hashlib.sha256(bundle.encode("utf-8", errors="replace")).hexdigest() if bundle else ""
    if bundle:
        with _CACHE_LOCK:
            if len(_CLIENT_CACHE) >= MAX_CACHE_ENTRIES:
                _CLIENT_CACHE.pop(next(iter(_CLIENT_CACHE)))
            _CLIENT_CACHE[key] = (bundle, source, digest)
    return bundle, source


def discover_server_guided_client_evidence(runtime: BGamingRuntime, data: dict[str, Any], *, timeout_s: float) -> dict[str, Any] | None:
    """Use a server response as an index for searching the official client bundle."""
    if not isinstance(data, dict):
        return None
    flow = data.get("flow")
    actions = flow.get("available_actions") if isinstance(flow, dict) else None
    if not isinstance(actions, list) or not actions:
        return None
    bundle, source = _load_client_bundle(runtime, timeout_s=timeout_s)
    evidence = analyze_server_response_against_bundle(data, bundle, source=source)
    if not bundle:
        evidence["warning"] = "provider client bundle unavailable for server-guided search"
    return evidence


__all__ = [
    "SCHEMA",
    "analyze_server_response_against_bundle",
    "begin_dynamic_contract_run",
    "discover_server_guided_client_evidence",
    "dynamic_action_option_fields",
    "dynamic_action_source",
    "dynamic_action_unresolved_variants",
    "dynamic_action_variants",
    "end_dynamic_contract_run",
    "remember_dynamic_evidence",
    "server_search_seeds",
]
