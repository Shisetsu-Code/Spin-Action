from __future__ import annotations

import json
import re
import threading
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from tester_spin.providers.bgaming.hyperhive_har import (
    HARHyperHiveEvidence,
    HARPlayTemplate,
    _request_json,
    _safe_mapping,
    current_thread_har_evidence,
)


_install_lock = threading.Lock()
_installed = False


def _variant_suffix(template: HARPlayTemplate, index: int) -> str:
    true_keys = [
        key
        for key, value in template.discriminator_custom_req.items()
        if value is True
    ]
    if len(true_keys) == 1:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", true_keys[0]).upper()

    true_req_keys = [
        key
        for key, value in template.discriminator_req.items()
        if value is True
    ]
    if len(true_req_keys) == 1:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", true_req_keys[0]).upper()
    return f"VARIANT_{index}"


def _purchase_template(
    req: dict[str, Any],
    evidence: HARHyperHiveEvidence,
    feature: str,
) -> HARPlayTemplate | None:
    variants = list(evidence.purchase_variants.get(feature) or [])
    if not variants:
        return evidence.purchases.get(feature)
    if len(variants) == 1:
        return variants[0]

    custom = req.get("custom_req")
    custom = custom if isinstance(custom, dict) else {}
    scored: list[tuple[int, HARPlayTemplate]] = []

    for template in variants:
        score = 0
        rejected = False

        for key, expected in template.discriminator_req.items():
            if key not in req:
                continue
            if req[key] != expected:
                rejected = True
                break
            score += 1
        if rejected:
            continue

        for key, expected in template.discriminator_custom_req.items():
            if key not in custom:
                continue
            if custom[key] != expected:
                rejected = True
                break
            score += 1
        if not rejected:
            scored.append((score, template))

    if not scored:
        return None
    best_score = max(score for score, _template in scored)
    if best_score <= 0:
        return None
    best = [template for score, template in scored if score == best_score]
    return best[0] if len(best) == 1 else None


def _template_for_request(
    req: dict[str, Any],
    evidence: HARHyperHiveEvidence,
) -> HARPlayTemplate | None:
    feature = str(req.get("purchased_feature") or "").strip()
    custom = req.get("custom_req")
    custom = custom if isinstance(custom, dict) else {}
    action = str(req.get("action") or custom.get("action") or "").strip().casefold()

    if feature:
        return _purchase_template(req, evidence, feature)
    if action and action != "spin":
        return evidence.continuations.get(action)
    return evidence.spin


def _request_from_template(
    incoming_req: dict[str, Any],
    template: HARPlayTemplate,
    evidence: HARHyperHiveEvidence,
) -> dict[str, Any]:
    """Rebuild a request from the observed HAR contract.

    Dynamic credentials never come from the HAR. The wager itself is handled by
    _apply_observed_envelope so an exact captured play can retain the exact JSON
    scalar/type that the provider accepted.
    """
    req: dict[str, Any] = {}
    bet = incoming_req.get("bet")
    if isinstance(bet, (int, float)) and not isinstance(bet, bool):
        req["bet"] = bet

    if evidence.bet_type:
        req["bet_type"] = evidence.bet_type

    feature = str(incoming_req.get("purchased_feature") or "").strip()
    if feature:
        req["purchased_feature"] = feature

    for key, value in template.req_extras.items():
        req[key] = value

    incoming_custom = incoming_req.get("custom_req")
    incoming_custom = incoming_custom if isinstance(incoming_custom, dict) else {}
    incoming_action = str(
        incoming_req.get("action") or incoming_custom.get("action") or ""
    ).strip().casefold()

    if template.custom_req:
        # Do not rewrite HAR-observed stake/exponent values here. When exact play
        # evidence exists, these fields are part of the accepted wire shape.
        custom = dict(template.custom_req)
        if incoming_action and "action" in custom:
            custom["action"] = incoming_action
        req["custom_req"] = custom
    else:
        action = incoming_action or str(template.action or "").strip().casefold()
        if action:
            req["action"] = action

    return req


def _row_action(req: dict[str, Any]) -> str:
    custom = req.get("custom_req")
    custom = custom if isinstance(custom, dict) else {}
    return str(req.get("action") or custom.get("action") or "").strip().casefold()


@lru_cache(maxsize=64)
def _observed_play_rows(
    path_text: str,
    size: int,
    mtime_ns: int,
) -> tuple[dict[str, Any], ...]:
    """Cache sanitized play envelopes from a selected HAR.

    Tokens and captured state locks are deliberately discarded. We retain only
    request-shape evidence plus the accepted wager scalar.
    """
    del size, mtime_ns
    path = Path(path_text)
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return ()

    log = payload.get("log") if isinstance(payload, dict) else None
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        return ()

    rows: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        payload = _request_json(entry)
        if not isinstance(payload, dict):
            continue
        if str(payload.get("method") or "").casefold() != "play":
            continue
        params = payload.get("params")
        req = params.get("req") if isinstance(params, dict) else None
        if not isinstance(params, dict) or not isinstance(req, dict):
            continue

        safe_req = _safe_mapping(req)
        rows.append(
            {
                "req": safe_req,
                "feature": str(req.get("purchased_feature") or "").strip(),
                "action": _row_action(req),
                "state_lock_present": "state_lock" in params,
                "params_extras": _safe_mapping(
                    params,
                    excluded={"token", "req", "state_lock"},
                ),
            }
        )
    return tuple(rows)


def _rows_for_evidence(evidence: HARHyperHiveEvidence) -> tuple[dict[str, Any], ...]:
    if not evidence.path:
        return ()
    path = Path(evidence.path)
    try:
        stat = path.stat()
    except OSError:
        return ()
    if not path.is_file() or stat.st_size <= 0:
        return ()
    return _observed_play_rows(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def _row_matches_template(row: dict[str, Any], template: HARPlayTemplate) -> bool:
    req = row.get("req")
    if not isinstance(req, dict):
        return False

    feature = str(row.get("feature") or "")
    expected_feature = str(template.purchased_feature or "")
    if feature != expected_feature:
        return False

    action = str(row.get("action") or "").casefold()
    expected_action = str(template.action or "").casefold()
    if expected_action and action != expected_action:
        return False

    for key, expected in template.discriminator_req.items():
        if req.get(key) != expected:
            return False

    custom = req.get("custom_req")
    custom = custom if isinstance(custom, dict) else {}
    for key, expected in template.discriminator_custom_req.items():
        if custom.get(key) != expected:
            return False
    return True


def _observed_row(
    evidence: HARHyperHiveEvidence,
    template: HARPlayTemplate,
) -> dict[str, Any] | None:
    for row in _rows_for_evidence(evidence):
        if _row_matches_template(row, template):
            return row
    return None


def _safe_observed_bet(req: dict[str, Any]) -> tuple[bool, Any]:
    if "bet" not in req:
        return False, None
    bet = req.get("bet")
    if isinstance(bet, bool):
        return False, None
    if isinstance(bet, (int, float)):
        return True, bet
    if isinstance(bet, str) and 0 < len(bet) <= 80:
        return True, bet
    return False, None


def _apply_observed_envelope(
    out: dict[str, Any],
    incoming_params: dict[str, Any],
    rebuilt_req: dict[str, Any],
    row: dict[str, Any] | None,
) -> dict[str, Any]:
    """Restore the exact non-secret JSON envelope observed in the HAR."""
    if row is None:
        out["req"] = rebuilt_req
        return out

    observed_req = row.get("req")
    observed_req = observed_req if isinstance(observed_req, dict) else {}
    has_bet, observed_bet = _safe_observed_bet(observed_req)
    if has_bet:
        rebuilt_req["bet"] = observed_bet

        # stake is another serialization of the wager in some HyperHive clients.
        # When it was captured, keep its exact observed scalar/type as well.
        observed_custom = observed_req.get("custom_req")
        rebuilt_custom = rebuilt_req.get("custom_req")
        if (
            isinstance(observed_custom, dict)
            and isinstance(rebuilt_custom, dict)
            and "stake" in observed_custom
        ):
            rebuilt_custom = dict(rebuilt_custom)
            rebuilt_custom["stake"] = observed_custom["stake"]
            rebuilt_req["custom_req"] = rebuilt_custom

    for key, value in dict(row.get("params_extras") or {}).items():
        out[key] = value

    if bool(row.get("state_lock_present")):
        live_lock = incoming_params.get("state_lock", "")
        out["state_lock"] = "" if live_lock is None else live_lock
    else:
        # Exact HAR evidence says this serializer omitted the field.
        out.pop("state_lock", None)

    out["req"] = rebuilt_req
    return out


def apply_authoritative_har_wire(
    params: dict[str, Any],
    evidence: HARHyperHiveEvidence,
) -> dict[str, Any]:
    """Make an observed HAR play authoritative over live-JS heuristics.

    The captured token/state_lock values are never replayed. Everything else
    required to describe the play, including the observed wager scalar/type and
    the presence of an empty state_lock field, is reconstructed from the exact
    successful request whenever available.
    """
    if not evidence.usable:
        return params

    raw_req = params.get("req")
    if not isinstance(raw_req, dict):
        return params

    template = _template_for_request(raw_req, evidence)
    feature = str(raw_req.get("purchased_feature") or "").strip()

    if template is None:
        if feature and len(evidence.purchase_variants.get(feature) or []) > 1:
            raise ValueError(
                "BGaming HyperHive: HAR contiene varias variantes para "
                f"{feature!r}, pero el request no identifica una de forma unívoca."
            )
        return params

    out = dict(params)
    rebuilt_req = _request_from_template(raw_req, template, evidence)
    row = _observed_row(evidence, template)
    return _apply_observed_envelope(out, params, rebuilt_req, row)


def _mode_request_from_template(
    template: HARPlayTemplate,
    evidence: HARHyperHiveEvidence,
    *,
    feature: str = "",
) -> dict[str, Any]:
    seed: dict[str, Any] = {}
    if feature:
        seed["purchased_feature"] = feature
    if template.discriminator_req:
        seed.update(template.discriminator_req)
    if template.discriminator_custom_req:
        seed["custom_req"] = dict(template.discriminator_custom_req)

    request = _request_from_template(seed, template, evidence)
    # run_hyperhive_test supplies a wager before the authoritative HAR adapter
    # restores the exact captured scalar. Keep discovery metadata wager-neutral.
    request.pop("bet", None)
    return request


def merge_har_modes(
    modes: list[dict[str, Any]],
    evidence: HARHyperHiveEvidence,
) -> list[dict[str, Any]]:
    """Replace only modes for which the selected HAR has exact play evidence."""
    if not evidence.usable or evidence.spin is None:
        return modes

    observed_features = set(evidence.purchase_variants) | set(evidence.purchases)
    retained: list[dict[str, Any]] = []

    for mode in modes:
        mode_id = str(mode.get("id") or "")
        request = mode.get("request")
        request = request if isinstance(request, dict) else {}
        feature = str(request.get("purchased_feature") or "")
        if mode_id == "SPIN":
            continue
        if feature and feature in observed_features:
            continue
        retained.append(mode)

    merged: list[dict[str, Any]] = [
        {
            "id": "SPIN",
            "kind": "SPIN",
            "request": _mode_request_from_template(evidence.spin, evidence),
            "expected_multiplier": 1.0,
            "custom_req_profile": "",
            "executable": True,
            "discovery_state": "HAR_OBSERVED",
            "source": "selected-har",
        }
    ]

    for feature in sorted(observed_features):
        variants = list(evidence.purchase_variants.get(feature) or [])
        if not variants:
            single = evidence.purchases.get(feature)
            variants = [single] if single is not None else []

        for index, template in enumerate(variants, 1):
            mode_id = f"PURCHASE_{feature.upper()}"
            if len(variants) > 1:
                mode_id += f"_{_variant_suffix(template, index)}"
            merged.append(
                {
                    "id": mode_id,
                    "kind": "PURCHASE",
                    "request": _mode_request_from_template(
                        template,
                        evidence,
                        feature=feature,
                    ),
                    "expected_multiplier": None,
                    "custom_req_profile": "",
                    "executable": True,
                    "discovery_state": "HAR_OBSERVED",
                    "source": "selected-har",
                }
            )

    merged.extend(retained)
    return merged


def install_har_bridge() -> None:
    """Connect the selected per-game HAR to the existing HyperHive runtime."""
    global _installed
    with _install_lock:
        if _installed:
            return

        from tester_spin.providers.bgaming import hyperhive, hyperhive_wire

        original_apply_wire = hyperhive_wire.apply_observed_play_wire
        original_discover_modes = hyperhive.discover_modes_from_bundle
        original_discover_actions = hyperhive.discover_action_vocabulary
        original_rpc_id = hyperhive._hyperhive_rpc_id

        def bridged_apply_wire(params, profile):
            adapted = original_apply_wire(params, profile)
            evidence = current_thread_har_evidence()
            return apply_authoritative_har_wire(adapted, evidence)

        def bridged_discover_modes(*args, **kwargs):
            modes = original_discover_modes(*args, **kwargs)
            evidence = current_thread_har_evidence()
            return merge_har_modes(modes, evidence)

        def bridged_discover_actions(
            bundle_text: str,
            engine_contract: str = "",
        ) -> set[str]:
            actions = set(original_discover_actions(bundle_text, engine_contract))
            evidence = current_thread_har_evidence()
            if evidence.usable:
                actions.update(str(action).casefold() for action in evidence.actions)
            return actions

        def bridged_rpc_id(contract_text: str):
            evidence = current_thread_har_evidence()
            if evidence.usable:
                if evidence.rpc_id_profile == "zero":
                    return 0
                if evidence.rpc_id_profile == "uuid":
                    return str(uuid.uuid4())
            return original_rpc_id(contract_text)

        hyperhive_wire.apply_observed_play_wire = bridged_apply_wire
        hyperhive.discover_modes_from_bundle = bridged_discover_modes
        hyperhive.discover_action_vocabulary = bridged_discover_actions
        hyperhive._hyperhive_rpc_id = bridged_rpc_id
        _installed = True


__all__ = [
    "apply_authoritative_har_wire",
    "install_har_bridge",
    "merge_har_modes",
]
