from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tester_spin.providers.bgaming.hyperhive_transport import (
    install_hyperhive_transport_adapter,
)
from tester_spin.providers.bgaming.money_display import install_money_display_adapter


# Install provider-boundary display normalization before the later wire adapter
# captures BaseBGamingProvider.test_game. Protocol artifacts and calculations
# remain in exact backend minor units.
install_money_display_adapter()

# hyperhive_wire is installed later by bgaming.__init__; this transport wrapper
# is therefore retained inside that final RPC chain.
install_hyperhive_transport_adapter()


_HTTP_ERROR = re.compile(
    r"\]\s+(?P<mode>[A-Z0-9_]+)\s+\d+/\d+:\s+ERROR\s+HTTPError:\s+"
    r"(?P<status>\d{3})\s+Client Error",
    re.IGNORECASE,
)
_HYPERHIVE_ERROR = re.compile(
    r"\]\s+(?P<mode>[A-Z0-9_]+)\s+\d+/\d+:\s+ERROR\s+HyperHiveRPCError:",
    re.IGNORECASE,
)


def _latest_attempt_dir(game_dir: Path, mode_id: str) -> Path | None:
    candidates: list[Path] = []
    root = Path(game_dir) / "tests"
    if not root.is_dir():
        return None
    for path in root.glob(f"*/{mode_id}/attempt-*"):
        if path.is_dir():
            candidates.append(path)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
    )


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _safe_custom_values(custom: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "action",
        "exponent",
        "isNormalBuy",
        "isSuperBuy",
        "perLine",
        "selectedWinLines",
        "stake",
    }
    out: dict[str, Any] = {}
    for key, value in custom.items():
        if key not in allowed:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif key == "selectedWinLines" and isinstance(value, list):
            out[key] = list(value[:16])
    return out


def _hyperhive_context(game_dir: Path, mode_id: str) -> dict[str, Any] | None:
    attempt = _latest_attempt_dir(game_dir, mode_id)
    if attempt is None:
        return None
    request_path = attempt / "rpc-error-request.json"
    if not request_path.is_file():
        return None

    payload = _load_json(request_path)
    params = payload.get("params")
    params = params if isinstance(params, dict) else {}
    req = params.get("req")
    req = req if isinstance(req, dict) else {}
    custom = req.get("custom_req")
    custom = custom if isinstance(custom, dict) else {}

    return {
        "kind": "hyperhive_rpc_rejected",
        "mode": mode_id,
        "artifact": _relative(request_path, Path(game_dir)),
        "rpc_id_type": type(payload.get("id")).__name__,
        "params_keys": sorted(str(key) for key in params),
        "state_lock_present": "state_lock" in params,
        "req_keys": sorted(str(key) for key in req),
        "bet_type": req.get("bet_type"),
        "action": req.get("action"),
        "purchased_feature": req.get("purchased_feature"),
        "custom_keys": sorted(str(key) for key in custom),
        "custom_values": _safe_custom_values(custom),
    }


def _http_context(
    game_dir: Path,
    mode_id: str,
    status: int,
) -> dict[str, Any] | None:
    attempt = _latest_attempt_dir(game_dir, mode_id)
    if attempt is None:
        return None
    failures = sorted(
        attempt.glob(f"http-*-{status}.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not failures:
        return None

    failure = failures[0]
    prefix = "http-"
    suffix = f"-{status}.json"
    name = failure.name
    command = name[len(prefix) : -len(suffix)] if name.endswith(suffix) else "unknown"
    first_response = attempt / "step-001-response.json"
    first_request = attempt / "step-001-request.json"
    initial_accepted = bool(
        command != "spin"
        and first_response.is_file()
        and first_response.stat().st_size > 0
    )
    initial_shape: dict[str, Any] = {}
    if first_request.is_file():
        payload = _load_json(first_request)
        options = payload.get("options")
        if isinstance(options, dict):
            initial_shape["option_keys"] = sorted(str(key) for key in options)
            for key in (
                "purchased_feature",
                "purchased_feature_level",
                "volatility",
                "gold_symbols_count",
                "rows",
            ):
                value = options.get(key)
                if isinstance(value, (str, int, float, bool)):
                    initial_shape[key] = value

    evidence = _load_json(failure)
    return {
        "kind": "api_http_rejected",
        "mode": mode_id,
        "status": status,
        "failed_command": command,
        "initial_mode_accepted": initial_accepted,
        "initial_request": initial_shape,
        "evidence_keys": sorted(str(key) for key in evidence),
        "artifact": _relative(failure, Path(game_dir)),
    }


def diagnose_progress_event(game_dir: Path, message: str) -> dict[str, Any] | None:
    """Derive a precise failure explanation from artifacts already written by the runner."""
    text = str(message or "")

    if "HAR usado por runner:" in text and (
        "HAR_BOOTSTRAP_ONLY" in text or "HAR_NETWORK_ONLY" in text
    ):
        return {
            "kind": "har_not_protocol_usable",
            "message": (
                "HAR local sin play/spin: no aporta templates de ejecución; "
                "el runner está usando discovery de JS/init como fallback."
            ),
        }

    match = _HTTP_ERROR.search(text)
    if match:
        mode = match.group("mode").upper()
        status = int(match.group("status"))
        context = _http_context(Path(game_dir), mode, status)
        if context is None:
            return None
        command = str(context["failed_command"])
        if context["initial_mode_accepted"]:
            context["message"] = (
                f"{mode}: la entrada inicial del modo fue aceptada; "
                f"falló la continuación {command!r} con HTTP {status}. "
                f"Artefacto: {context['artifact']}."
            )
        else:
            context["message"] = (
                f"{mode}: falló el request inicial {command!r} con HTTP {status}. "
                f"Artefacto: {context['artifact']}."
            )
        return context

    match = _HYPERHIVE_ERROR.search(text)
    if match:
        mode = match.group("mode").upper()
        context = _hyperhive_context(Path(game_dir), mode)
        if context is None:
            return None
        context["message"] = (
            f"{mode} outbound rechazado: req_keys={context['req_keys']}, "
            f"bet_type={context['bet_type']!r}, action={context['action']!r}, "
            f"custom_keys={context['custom_keys']}, "
            f"custom={context['custom_values']}, "
            f"state_lock={'sí' if context['state_lock_present'] else 'no'}. "
            f"Artefacto: {context['artifact']}."
        )
        return context

    return None


__all__ = ["diagnose_progress_event"]
