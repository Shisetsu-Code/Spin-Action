from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


ACTION_RE = re.compile(r"^do[A-Z][A-Za-z0-9_]*$")

FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "free_spins": (
        "fs",
        "fsmax",
        "fs_total",
        "fsleft",
        "fs_left",
        "fsmul",
        "fsres",
        "fsres_total",
    ),
    "respins": (
        "rs_c",
        "rs_t",
        "rs_more",
        "rs_p",
        "rsc",
        "respins",
        "respin",
    ),
    "purchase": (
        "puri",
        "purtr",
        "pur",
        "purInit",
        "purInit_e",
    ),
    "bonus": (
        "bonus",
        "bonus_id",
        "bonusType",
        "bonus_type",
        "feature",
        "featureType",
        "feature_type",
    ),
}

VALUE_GROUPS: dict[str, tuple[str, ...]] = {
    "win": ("tw", "w", "rw", "win", "totalWin", "total_win", "gwm"),
    "balance": ("balance", "cash", "bonusBalance", "bonus_balance"),
    "round": ("roundId", "round_id", "rid", "index", "counter"),
}

ACTION_KEYS = {
    "action",
    "nextaction",
    "next_action",
    "command",
    "cmd",
    "requestaction",
    "request_action",
}


def _present(value: Any) -> bool:
    return value not in (None, "", "0", "0.0", "false", "False", "null", "None")


def _embedded_objects(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _embedded_objects(nested)
        return
    if isinstance(value, list):
        yield value
        for nested in value:
            yield from _embedded_objects(nested)
        return
    if not isinstance(value, str):
        return

    text = value.strip()
    if not text or text[0] not in "[{":
        return
    try:
        parsed = json.loads(text)
    except Exception:
        return
    yield parsed
    yield from _embedded_objects(parsed)


def _explicit_actions(fields: dict[str, str]) -> list[dict[str, str]]:
    discovered: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def inspect(mapping: dict[str, Any], source: str) -> None:
        for key, raw_value in mapping.items():
            key_text = str(key)
            key_norm = key_text.replace("-", "_").casefold()
            value = str(raw_value).strip() if raw_value is not None else ""
            if key_norm in ACTION_KEYS and ACTION_RE.match(value):
                item = (source, key_text, value)
                if item not in seen:
                    seen.add(item)
                    discovered.append({"source": source, "field": key_text, "action": value})

    inspect(fields, "wire")
    for field_name, raw in fields.items():
        for embedded in _embedded_objects(raw):
            if isinstance(embedded, dict):
                inspect(embedded, f"embedded:{field_name}")

    # Some payloads put an action token in a generic string instead of an action
    # property. Preserve it as evidence, but do not treat arbitrary text as a safe
    # command to execute.
    for field_name, raw in fields.items():
        value = str(raw).strip()
        if ACTION_RE.match(value):
            item = ("wire-value", str(field_name), value)
            if item not in seen:
                seen.add(item)
                discovered.append({"source": "wire-value", "field": str(field_name), "action": value})

    return discovered


def _group_presence(fields: dict[str, str], groups: dict[str, tuple[str, ...]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    lowered = {str(key).casefold(): str(key) for key in fields}
    for group, candidates in groups.items():
        present: list[str] = []
        for candidate in candidates:
            real_key = lowered.get(candidate.casefold())
            if real_key is not None and _present(fields.get(real_key)):
                present.append(real_key)
        if present:
            result[group] = present
    return result


def analyze_response(fields: dict[str, str]) -> dict[str, Any]:
    """Classify one Pragmatic gameService response without discarding unknowns.

    The analyzer deliberately separates observation from execution. Unknown
    continuation states are described and fingerprinted so they can be compared
    across games. Only states already implemented by the transport state machine
    are marked with an automatic handler.
    """
    normalized = {str(key): "" if value is None else str(value) for key, value in fields.items()}
    na = normalized.get("na", "").strip().lower()
    feature_groups = _group_presence(normalized, FEATURE_GROUPS)
    value_groups = _group_presence(normalized, VALUE_GROUPS)
    explicit_actions = _explicit_actions(normalized)
    feature_active = bool(feature_groups)

    error_value = (
        normalized.get("error")
        or normalized.get("err")
        or normalized.get("errorCode")
        or ""
    )

    automatic_handler = ""
    terminal_hint = False
    if error_value not in ("", "0"):
        state_kind = "server_error"
    elif na == "b":
        state_kind = "bonus_required"
        automatic_handler = "doBonus"
    elif na in {"cb", "bc"}:
        state_kind = "bonus_collect_required"
        automatic_handler = "doCollectBonus"
    elif na == "c":
        state_kind = "collect_required"
        automatic_handler = "doCollect"
    elif na == "s" and feature_active:
        state_kind = "feature_spin_continuation"
        automatic_handler = "doSpin"
    elif na in {"", "s"}:
        state_kind = "terminal_or_idle"
        terminal_hint = True
    elif explicit_actions:
        state_kind = "explicit_action_observed"
    elif feature_active:
        state_kind = "feature_continuation_unknown"
    else:
        state_kind = "provider_state_unknown"

    important = {
        "na": na,
        "error": error_value,
        "feature_groups": sorted(feature_groups),
        "explicit_actions": sorted(item["action"] for item in explicit_actions),
        "keys": sorted(normalized),
    }
    signature = hashlib.sha256(
        json.dumps(important, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]

    known_keys = {"na", "error", "err", "errorCode"}
    for names in FEATURE_GROUPS.values():
        known_keys.update(names)
    for names in VALUE_GROUPS.values():
        known_keys.update(names)
    known_folded = {key.casefold() for key in known_keys}
    unclassified_keys = sorted(key for key in normalized if key.casefold() not in known_folded)

    return {
        "schema": "tester-spin/pragmatic-response-analysis/v1",
        "state_kind": state_kind,
        "state_signature": signature,
        "na": na,
        "terminal_hint": terminal_hint,
        "automatic_handler": automatic_handler,
        "feature_active": feature_active,
        "feature_groups": feature_groups,
        "value_groups": value_groups,
        "explicit_actions": explicit_actions,
        "server_error": error_value,
        "field_count": len(normalized),
        "fields": normalized,
        "unclassified_keys": unclassified_keys,
    }


def summarize_analysis_files(run_root: Path) -> dict[str, Any]:
    state_counts: Counter[str] = Counter()
    signature_counts: Counter[str] = Counter()
    na_counts: Counter[str] = Counter()
    explicit_actions: Counter[str] = Counter()
    unknown_examples: dict[str, dict[str, Any]] = {}
    files = sorted(run_root.rglob("step-*.analysis.json")) if run_root.exists() else []

    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        state = str(payload.get("state_kind") or "unknown")
        signature = str(payload.get("state_signature") or "")
        na = str(payload.get("na") or "")
        state_counts[state] += 1
        if signature:
            signature_counts[signature] += 1
        na_counts[na] += 1
        for item in payload.get("explicit_actions") or []:
            if isinstance(item, dict) and item.get("action"):
                explicit_actions[str(item["action"])] += 1
        if state in {"provider_state_unknown", "feature_continuation_unknown", "explicit_action_observed"}:
            key = signature or f"{state}:{na}"
            unknown_examples.setdefault(
                key,
                {
                    "state_kind": state,
                    "na": na,
                    "signature": signature,
                    "file": str(path.relative_to(run_root)),
                    "unclassified_keys": payload.get("unclassified_keys") or [],
                    "explicit_actions": payload.get("explicit_actions") or [],
                    "feature_groups": payload.get("feature_groups") or {},
                },
            )

    return {
        "schema": "tester-spin/pragmatic-protocol-observations/v1",
        "responses_analyzed": sum(state_counts.values()),
        "state_counts": dict(sorted(state_counts.items())),
        "na_counts": dict(sorted(na_counts.items())),
        "signature_counts": dict(sorted(signature_counts.items())),
        "explicit_actions": dict(sorted(explicit_actions.items())),
        "unknown_signatures": list(unknown_examples.values()),
    }
