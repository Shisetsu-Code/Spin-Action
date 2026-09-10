from __future__ import annotations

import base64
import json
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tester_spin.providers.bgaming import hyperhive_har


_install_lock = threading.Lock()
_installed = False

_PROVIDER_HOST_SUFFIX = ".bgaming-network.com"
_ROLE_BASENAMES = {
    "client.min.js",
    "common.min.js",
    "game.min.js",
    "integration.min.js",
}
_CONTRACT_MARKERS = (
    "jsonrpc",
    "state_lock",
    "custom_req",
    "formattedRequest.params",
    "purchased_feature",
    "customizeFeatureBuyRequestData",
    "AdditionalData.params",
    "isNormalBuy",
    "isSuperBuy",
    "nextAction",
)
_MAX_SCRIPT_BYTES = 3 * 1024 * 1024
_MAX_TOTAL_BYTES = 12 * 1024 * 1024


def _current_har_path() -> Path | None:
    raw = str(getattr(hyperhive_har._thread_state, "har_path", "") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def _provider_js_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    host = str(parsed.hostname or "").casefold()
    path = str(parsed.path or "").casefold()
    return bool(
        parsed.scheme in {"http", "https"}
        and (host == "bgaming-network.com" or host.endswith(_PROVIDER_HOST_SUFFIX))
        and path.endswith(".js")
    )


def _decode_har_content(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    text = content.get("text")
    if not isinstance(text, str) or not text:
        return ""
    if str(content.get("encoding") or "").casefold() == "base64":
        try:
            raw = base64.b64decode(text, validate=False)
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return text


def _is_contract_script(url: str, text: str) -> bool:
    basename = urlparse(url).path.rsplit("/", 1)[-1].casefold()
    if basename in _ROLE_BASENAMES:
        return True
    return any(marker in text for marker in _CONTRACT_MARKERS)


@lru_cache(maxsize=64)
def _extract_cached(path_text: str, size: int, mtime_ns: int) -> str:
    del size, mtime_ns
    path = Path(path_text)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    log = payload.get("log") if isinstance(payload, dict) else None
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        return ""

    parts: list[str] = []
    seen: set[tuple[str, int]] = set()
    total = 0

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request")
        response = entry.get("response")
        if not isinstance(request, dict) or not isinstance(response, dict):
            continue
        url = str(request.get("url") or "")
        if not _provider_js_url(url):
            continue

        text = _decode_har_content(response.get("content"))
        if not text or not _is_contract_script(url, text):
            continue

        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) > _MAX_SCRIPT_BYTES:
            continue
        if total + len(encoded) > _MAX_TOTAL_BYTES:
            break

        fingerprint = (urlparse(url).path.casefold(), hash(text))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        parts.append(text)
        total += len(encoded)

    return "\n".join(parts)


def current_har_script_contract() -> str:
    path = _current_har_path()
    if path is None:
        return ""
    try:
        stat = path.stat()
    except OSError:
        return ""
    return _extract_cached(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def _apply_profile(params: dict[str, Any], profile: Any) -> dict[str, Any]:
    """Apply HAR-script wire facts after the normal live-client adapter.

    This path is used only when the selected HAR has no play request. It never
    invents literals: every custom field comes from JavaScript embedded in that
    HAR. A HAR with real play data remains stronger and is handled by the exact
    HAR bridge installed before this module.
    """
    out = dict(params)
    raw_req = out.get("req")
    if not isinstance(raw_req, dict):
        return out
    req = dict(raw_req)

    if str(getattr(profile, "bet_type", "") or ""):
        req["bet_type"] = profile.bet_type

    if not bool(getattr(profile, "custom_req", False)):
        out["req"] = req
        return out

    existing = req.get("custom_req")
    existing = dict(existing) if isinstance(existing, dict) else {}
    action = str(
        req.pop("action", "")
        or existing.get("action")
        or "spin"
    )

    custom = dict(existing)
    if action.casefold() == "spin":
        for key, value in dict(getattr(profile, "custom_literals", {}) or {}).items():
            custom.setdefault(key, value)
    if bool(getattr(profile, "custom_action", False)):
        custom["action"] = action
    if bool(getattr(profile, "custom_exponent", False)):
        custom["exponent"] = int(getattr(profile, "exponent", 2) or 2)
    if (
        bool(getattr(profile, "custom_stake_on_spin", False))
        and action.casefold() == "spin"
        and isinstance(req.get("bet"), (int, float))
    ):
        custom["stake"] = req["bet"]

    if custom:
        req["custom_req"] = custom
    out["req"] = req
    return out


def _merge_script_modes(
    modes: list[dict[str, Any]],
    profile: Any,
    contract_text: str,
    *,
    purchase_feature_names,
    variant_suffix,
) -> list[dict[str, Any]]:
    variants = list(getattr(profile, "purchase_custom_variants", []) or [])
    if not variants:
        return modes

    features = sorted(str(x) for x in purchase_feature_names(contract_text) if str(x))
    existing_features = sorted(
        {
            str((mode.get("request") or {}).get("purchased_feature") or "")
            for mode in modes
            if isinstance(mode.get("request"), dict)
            and str((mode.get("request") or {}).get("purchased_feature") or "")
        }
    )
    candidates = sorted(set(features) | set(existing_features))
    if len(candidates) != 1:
        return modes
    feature = candidates[0]

    base = next((mode for mode in modes if str(mode.get("id") or "") == "SPIN"), None)
    base_executable = bool(base and base.get("executable"))
    base_bet_type = ""
    if isinstance(base, dict) and isinstance(base.get("request"), dict):
        base_bet_type = str(base["request"].get("bet_type") or "")

    filtered: list[dict[str, Any]] = []
    for mode in modes:
        request = mode.get("request")
        request = request if isinstance(request, dict) else {}
        if str(request.get("purchased_feature") or "") == feature:
            continue
        filtered.append(mode)

    for index, variant in enumerate(variants, 1):
        custom = dict(variant)
        if bool(getattr(profile, "custom_action", False)):
            custom["action"] = "spin"
        if bool(getattr(profile, "custom_exponent", False)):
            custom["exponent"] = int(getattr(profile, "exponent", 2) or 2)
        request: dict[str, Any] = {
            "purchased_feature": feature,
            "custom_req": custom,
        }
        if base_bet_type:
            request["bet_type"] = base_bet_type
        filtered.append(
            {
                "id": f"PURCHASE_{feature.upper()}_{variant_suffix(variant, index)}",
                "kind": "PURCHASE",
                "request": request,
                "expected_multiplier": None,
                "source": "selected-har-script-contract",
                "executable": base_executable,
                "discovery_state": "HAR_SCRIPT_OBSERVED_VARIANT",
            }
        )
    return filtered


def install_har_script_bridge() -> None:
    """Use embedded JS from bootstrap-only HARs as secondary HyperHive evidence."""
    global _installed
    with _install_lock:
        if _installed:
            return

        from tester_spin.providers.bgaming import hyperhive, hyperhive_wire

        original_apply = hyperhive_wire.apply_observed_play_wire
        original_modes = hyperhive.discover_modes_from_bundle
        original_actions = hyperhive.discover_action_vocabulary

        def bridged_apply(params, profile):
            adapted = original_apply(params, profile)
            exact = hyperhive_har.current_thread_har_evidence()
            if exact.usable:
                return adapted
            contract = current_har_script_contract()
            if not contract:
                return adapted
            har_profile = hyperhive_wire.analyze_engine_wire(contract)
            return _apply_profile(adapted, har_profile)

        def bridged_modes(*args, **kwargs):
            modes = original_modes(*args, **kwargs)
            exact = hyperhive_har.current_thread_har_evidence()
            if exact.usable:
                return modes
            contract = current_har_script_contract()
            if not contract:
                return modes
            har_profile = hyperhive_wire.analyze_engine_wire(contract)
            return _merge_script_modes(
                modes,
                har_profile,
                contract,
                purchase_feature_names=hyperhive_wire._purchase_feature_names,
                variant_suffix=hyperhive_wire._variant_suffix,
            )

        def bridged_actions(bundle_text: str, engine_contract: str = "") -> set[str]:
            actions = set(original_actions(bundle_text, engine_contract))
            exact = hyperhive_har.current_thread_har_evidence()
            if exact.usable:
                return actions
            contract = current_har_script_contract()
            if contract:
                actions.update(hyperhive_wire._client_action_enum_values(contract))
            return actions

        hyperhive_wire.apply_observed_play_wire = bridged_apply
        hyperhive.discover_modes_from_bundle = bridged_modes
        hyperhive.discover_action_vocabulary = bridged_actions
        _installed = True


__all__ = [
    "current_har_script_contract",
    "install_har_script_bridge",
]
