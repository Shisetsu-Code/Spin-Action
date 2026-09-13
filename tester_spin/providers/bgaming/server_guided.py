from __future__ import annotations

import hashlib
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


def _option_fields_near(bundle: str, occurrence) -> list[str]:
    window = (bundle or "")[occurrence.start(): occurrence.start() + 1600]
    fields: list[str] = []
    for match in re.finditer(r"(?:[\"']?options[\"']?)\s*:\s*\{([^{}]{0,800})\}", window):
        for pair in re.finditer(r"(?:[\"']?)([A-Za-z_][A-Za-z0-9_]*)(?:[\"']?)\s*:", match.group(1)):
            field = pair.group(1)
            if field not in fields:
                fields.append(field)
        if fields:
            break
    return fields


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
        windows[action] = []
        for occurrence in occurrences:
            for field in _option_fields_near(bundle, occurrence):
                if field not in fields:
                    fields.append(field)
            windows[action].append((bundle or "")[max(0, occurrence.start() - 1200): occurrence.start() + 1800])
        rows.append({
            "action": action,
            "advertised": True,
            "client_command_literal_hits": len(occurrences),
            "option_fields": fields,
            "serializer_shape_proven": bool(occurrences and fields),
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
    "discover_server_guided_client_evidence",
    "server_search_seeds",
]
