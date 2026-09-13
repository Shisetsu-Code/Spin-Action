"""Provider-neutral, conservative sanitization and structural identities."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_PRIVATE = re.compile(r"token|cookie|csrf|authorization|password|secret|session|t_key|launch_url|api.?key|credential", re.I)
_RUNTIME = re.compile(r"round.*id|action.*id|timestamp|request.*id|nonce|^uuid$", re.I)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def field_role(path: str) -> str:
    key = path.rsplit(".", 1)[-1]
    if _PRIVATE.search(key):
        return "session"
    if _RUNTIME.search(key):
        return "runtime"
    return "unknown"


def sanitize(value: Any, path: str = "") -> Any:
    role = field_role(path)
    if role != "unknown":
        return f"[runtime:{path.rsplit('.', 1)[-1]}]"
    if isinstance(value, dict):
        return {safe_key(k): sanitize(v, f"{path}.{k}".strip(".")) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, path + "[]") for v in value]
    if isinstance(value, str):
        if "://" in value or value.lower().startswith("bearer "):
            return "[runtime:opaque-reference]"
        return _UUID.sub("[runtime:uuid]", value)
    return value


def safe_key(value: Any) -> str:
    text = str(value)
    return _UUID.sub("[runtime:mapping-key]", text)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def identity(kind: str, *parts: Any) -> str:
    return kind + ":" + hashlib.sha256(canonical(sanitize(parts)).encode()).hexdigest()[:24]


def bounded_add(items: list, value: Any, limit: int = 3) -> None:
    if value not in items and len(items) < limit:
        items.append(value)
