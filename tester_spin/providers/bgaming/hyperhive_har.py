from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any


_DYNAMIC_OR_SECRET_KEYS = {
    "id",
    "token",
    "state_lock",
    "session",
    "session_id",
    "csrf",
    "nonce",
    "timestamp",
    "seed",
}


def _safe_template_value(key: str, value: Any) -> Any:
    lowered = str(key or "").casefold()
    if (
        lowered in _DYNAMIC_OR_SECRET_KEYS
        or "token" in lowered
        or "secret" in lowered
        or "password" in lowered
        or "session" in lowered
    ):
        raise ValueError("dynamic or sensitive field")
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 200:
            raise ValueError("oversized string")
        return value
    if isinstance(value, list):
        if len(value) > 64:
            raise ValueError("oversized list")
        return [
            _safe_template_value(f"{key}[]", item)
            for item in value
        ]
    if isinstance(value, dict):
        if len(value) > 64:
            raise ValueError("oversized object")
        out: dict[str, Any] = {}
        for child_key, child_value in value.items():
            try:
                out[str(child_key)] = _safe_template_value(
                    str(child_key), child_value
                )
            except ValueError:
                continue
        return out
    raise ValueError("unsupported template value")


def _safe_mapping(value: Any, *, excluded: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    excluded_lower = {str(item).casefold() for item in (excluded or set())}
    out: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        if key_text.casefold() in excluded_lower:
            continue
        try:
            out[key_text] = _safe_template_value(key_text, item)
        except ValueError:
            continue
    return out


def _common_mapping(values: list[dict[str, Any]]) -> dict[str, Any]:
    if not values:
        return {}
    common = dict(values[0])
    for mapping in values[1:]:
        for key in list(common):
            if key not in mapping or mapping[key] != common[key]:
                common.pop(key, None)
    return common


@dataclass(slots=True)
class HARPlayTemplate:
    action: str = ""
    purchased_feature: str = ""
    req_extras: dict[str, Any] = field(default_factory=dict)
    custom_req: dict[str, Any] = field(default_factory=dict)
    observations: int = 0


@dataclass(slots=True)
class HARHyperHiveEvidence:
    path: str = ""
    play_count: int = 0
    bet_type: str = ""
    rpc_id_profile: str = ""
    actions: set[str] = field(default_factory=set)
    purchase_features: set[str] = field(default_factory=set)
    spin: HARPlayTemplate | None = None
    purchases: dict[str, HARPlayTemplate] = field(default_factory=dict)
    continuations: dict[str, HARPlayTemplate] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.play_count > 0 and self.spin is not None


def _request_json(entry: dict[str, Any]) -> dict[str, Any] | None:
    request = entry.get("request")
    if not isinstance(request, dict) or str(request.get("method") or "").upper() != "POST":
        return None
    post_data = request.get("postData")
    if not isinstance(post_data, dict):
        return None
    text = post_data.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        payload = json.loads(text)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _group_template(rows: list[dict[str, Any]]) -> HARPlayTemplate | None:
    if not rows:
        return None
    actions = {
        str(row.get("action") or "").strip()
        for row in rows
        if str(row.get("action") or "").strip()
    }
    features = {
        str(row.get("purchased_feature") or "").strip()
        for row in rows
        if str(row.get("purchased_feature") or "").strip()
    }
    return HARPlayTemplate(
        action=next(iter(actions)) if len(actions) == 1 else "",
        purchased_feature=next(iter(features)) if len(features) == 1 else "",
        req_extras=_common_mapping(
            [dict(row.get("req_extras") or {}) for row in rows]
        ),
        custom_req=_common_mapping(
            [dict(row.get("custom_req") or {}) for row in rows]
        ),
        observations=len(rows),
    )


@lru_cache(maxsize=128)
def _analyze_cached(path_text: str, size: int, mtime_ns: int) -> HARHyperHiveEvidence:
    del size, mtime_ns
    path = Path(path_text)
    evidence = HARHyperHiveEvidence(path=str(path))
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return evidence

    log = payload.get("log") if isinstance(payload, dict) else None
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        return evidence

    rows: list[dict[str, Any]] = []
    rpc_ids: list[Any] = []
    bet_types: list[str] = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request_payload = _request_json(entry)
        if not isinstance(request_payload, dict):
            continue
        if str(request_payload.get("method") or "").casefold() != "play":
            continue
        params = request_payload.get("params")
        req = params.get("req") if isinstance(params, dict) else None
        if not isinstance(req, dict):
            continue

        rpc_ids.append(request_payload.get("id"))
        bet_type = str(req.get("bet_type") or "").strip()
        if bet_type:
            bet_types.append(bet_type)

        custom_req = _safe_mapping(req.get("custom_req"))
        action = str(
            custom_req.get("action")
            or req.get("action")
            or ""
        ).strip()
        feature = str(req.get("purchased_feature") or "").strip()
        if action:
            evidence.actions.add(action.casefold())
        if feature:
            evidence.purchase_features.add(feature)

        rows.append(
            {
                "action": action,
                "purchased_feature": feature,
                "req_extras": _safe_mapping(
                    req,
                    excluded={
                        "bet",
                        "bet_type",
                        "action",
                        "custom_req",
                        "purchased_feature",
                    },
                ),
                "custom_req": custom_req,
            }
        )

    evidence.play_count = len(rows)
    if not rows:
        return evidence

    if bet_types and len(set(bet_types)) == 1:
        evidence.bet_type = bet_types[0]

    if rpc_ids and all(isinstance(value, int) and value == 0 for value in rpc_ids):
        evidence.rpc_id_profile = "zero"
    elif rpc_ids and all(
        isinstance(value, str)
        and bool(value)
        for value in rpc_ids
    ):
        try:
            for value in rpc_ids:
                uuid.UUID(str(value))
        except Exception:
            pass
        else:
            evidence.rpc_id_profile = "uuid"

    base_rows = [
        row for row in rows
        if not row["purchased_feature"]
        and str(row["action"] or "").casefold() in {"", "spin"}
    ]
    evidence.spin = _group_template(base_rows)

    for feature in sorted(evidence.purchase_features):
        template = _group_template(
            [row for row in rows if row["purchased_feature"] == feature]
        )
        if template is not None:
            evidence.purchases[feature] = template

    for action in sorted(evidence.actions):
        if action in {"", "spin"}:
            continue
        template = _group_template(
            [
                row for row in rows
                if str(row["action"] or "").casefold() == action
                and not row["purchased_feature"]
            ]
        )
        if template is not None:
            evidence.continuations[action] = template

    return evidence


def analyze_hyperhive_har(path: Path | str | None) -> HARHyperHiveEvidence:
    if path is None:
        return HARHyperHiveEvidence()
    candidate = Path(path)
    try:
        stat = candidate.stat()
    except OSError:
        return HARHyperHiveEvidence(path=str(candidate))
    if not candidate.is_file() or stat.st_size <= 0:
        return HARHyperHiveEvidence(path=str(candidate))
    return _analyze_cached(str(candidate.resolve()), stat.st_size, stat.st_mtime_ns)


def apply_har_play_wire(
    params: dict[str, Any],
    evidence: HARHyperHiveEvidence,
) -> dict[str, Any]:
    """Adapt one play request using only exact wire values observed in the HAR."""
    if not evidence.usable:
        return params
    out = dict(params)
    raw_req = out.get("req")
    if not isinstance(raw_req, dict):
        return out
    req = dict(raw_req)

    if evidence.bet_type:
        req["bet_type"] = evidence.bet_type

    requested_action = str(req.get("action") or "").strip().casefold()
    feature = str(req.get("purchased_feature") or "").strip()
    template: HARPlayTemplate | None = None
    if feature:
        template = evidence.purchases.get(feature)
    elif requested_action and requested_action != "spin":
        template = evidence.continuations.get(requested_action)
    else:
        template = evidence.spin

    if template is not None:
        for key, value in template.req_extras.items():
            req[key] = value

        if template.custom_req:
            custom = dict(template.custom_req)
            action = requested_action or str(custom.get("action") or "spin").casefold()
            if "action" in custom:
                custom["action"] = action
            if "stake" in custom and isinstance(req.get("bet"), (int, float)):
                custom["stake"] = req["bet"]
            req["custom_req"] = custom
            req.pop("action", None)

    out["req"] = req
    return out


_thread_state = threading.local()


def set_thread_har_path(path: Path | str | None) -> None:
    _thread_state.har_path = str(path) if path else ""


def clear_thread_har_path() -> None:
    _thread_state.har_path = ""


def current_thread_har_evidence() -> HARHyperHiveEvidence:
    return analyze_hyperhive_har(getattr(_thread_state, "har_path", "") or None)


__all__ = [
    "HARHyperHiveEvidence",
    "HARPlayTemplate",
    "analyze_hyperhive_har",
    "apply_har_play_wire",
    "clear_thread_har_path",
    "current_thread_har_evidence",
    "set_thread_har_path",
]
