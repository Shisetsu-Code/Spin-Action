from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tester_spin.models import GameTestResult
from tester_spin.providers.bgaming import execution as _execution
from tester_spin.providers.bgaming import hyperhive as _hyperhive
from tester_spin.providers.bgaming import runtime as _runtime


WAGER_CATALOG_SCHEMA = "tester-spin/bgaming-wager-catalog/v1"
RESERVED_PURCHASE_OPTION_FIELDS = {
    "purchased_feature",
    "purchased_feature_level",
}

_LOCAL = threading.local()
_ORIGINAL_DISCOVER_PROFILE = _execution.discover_profile
_ORIGINAL_DISCOVER_PURCHASE_MODES = _execution.discover_purchase_modes
_ORIGINAL_RESOLVE_BASE_BET = _execution.resolve_base_bet
_ORIGINAL_HYPERHIVE_RPC = _hyperhive._rpc
_INSTALLED = False


@dataclass(slots=True)
class WagerPlan:
    default_bet: float | None
    coverage_bet: float | None
    available_bets: list[float]
    balance: float | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_bet": self.default_bet,
            "coverage_bet": self.coverage_bet,
            "available_bets": list(self.available_bets),
            "balance": self.balance,
            "source": self.source,
        }


def _positive_numbers(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    out: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            continue
        number = float(item)
        if number <= 0:
            continue
        if number not in out:
            out.append(number)
    return sorted(out)


def wager_plan_from_init(data: dict[str, Any]) -> WagerPlan:
    """Return provider-advertised wager metadata and the safest coverage wager.

    Coverage always prefers the lowest wager explicitly advertised by the
    provider. The provider's UI/default wager is preserved separately so
    Tester-Spin can later reproduce the original client configuration without
    making expensive feature purchases during protocol traversal.
    """
    if not isinstance(data, dict):
        return WagerPlan(None, None, [], None, "unknown")

    # Normal API-v2/legacy response.
    options = data.get("options")
    if isinstance(options, dict):
        default_raw = options.get("default_bet")
        default_bet = (
            float(default_raw)
            if isinstance(default_raw, (int, float))
            and not isinstance(default_raw, bool)
            and float(default_raw) > 0
            else None
        )
        available = _positive_numbers(options.get("available_bets"))
        source = "options.available_bets:min" if available else ""

        if not available:
            available = _positive_numbers(options.get("line_bets"))
            if available:
                source = "options.line_bets:min"

        if not available:
            bet_raw = options.get("bet")
            if (
                isinstance(bet_raw, (int, float))
                and not isinstance(bet_raw, bool)
                and float(bet_raw) > 0
            ):
                available = [float(bet_raw)]
                source = "options.bet"

        if not available and default_bet is not None:
            available = [default_bet]
            source = "options.default_bet"

        coverage_bet = min(available) if available else default_bet
        balance = _runtime.balance_total(data)
        return WagerPlan(
            default_bet=default_bet,
            coverage_bet=coverage_bet,
            available_bets=available,
            balance=float(balance) if isinstance(balance, (int, float)) else None,
            source=source or "unknown",
        )

    # HyperHive JSON-RPC init response.
    result = data.get("result")
    if isinstance(result, dict):
        config = result.get("config")
        if isinstance(config, dict):
            default_raw = config.get("default_bet")
            default_bet = (
                float(default_raw)
                if isinstance(default_raw, (int, float))
                and not isinstance(default_raw, bool)
                and float(default_raw) > 0
                else None
            )
            advertised_limits = _positive_numbers(config.get("bet_limits"))
            available = list(advertised_limits)
            if not available and default_bet is not None:
                available = [default_bet]
            coverage_bet = min(available) if available else default_bet
            balance_raw = result.get("balance")
            balance = (
                float(balance_raw)
                if isinstance(balance_raw, (int, float))
                and not isinstance(balance_raw, bool)
                else None
            )
            return WagerPlan(
                default_bet=default_bet,
                coverage_bet=coverage_bet,
                available_bets=available,
                balance=balance,
                source=(
                    "result.config.bet_limits:min"
                    if advertised_limits
                    else "result.config.default_bet"
                ),
            )

    return WagerPlan(None, None, [], None, "unknown")


def resolve_coverage_bet(data: dict[str, Any]) -> tuple[int | float | None, str]:
    plan = wager_plan_from_init(data)
    if plan.coverage_bet is not None:
        value = plan.coverage_bet
        return (int(value) if value.is_integer() else value), plan.source
    return _ORIGINAL_RESOLVE_BASE_BET(data)


def prepare_hyperhive_init_for_coverage(
    data: dict[str, Any],
) -> tuple[dict[str, Any], WagerPlan]:
    """Return a copy whose execution default is the provider-advertised minimum.

    The original provider response is never mutated. The raw response is kept
    separately and restored to init-response.json after the run, so lowering the
    coverage wager cannot contaminate evidence or future emulation contracts.
    """
    plan = wager_plan_from_init(data)
    adjusted = copy.deepcopy(data)
    if plan.coverage_bet is None:
        return adjusted, plan
    result = adjusted.get("result")
    config = result.get("config") if isinstance(result, dict) else None
    if not isinstance(config, dict):
        return adjusted, plan
    value = plan.coverage_bet
    config["default_bet"] = int(value) if value.is_integer() else value
    return adjusted, plan


def _hyperhive_rpc_policy(*args, **kwargs):
    response, payload, data = _ORIGINAL_HYPERHIVE_RPC(*args, **kwargs)
    method = str(args[1] if len(args) > 1 else kwargs.get("method") or "")
    if method != "init" or not bool(getattr(_LOCAL, "coverage_active", False)):
        return response, payload, data

    _LOCAL.hyperhive_raw_init = copy.deepcopy(data)
    adjusted, plan = prepare_hyperhive_init_for_coverage(data)
    _LOCAL.hyperhive_wager_plan = plan
    return response, payload, adjusted


def scoped_choice_domains(
    profile: dict[str, Any],
    original: Callable[[dict[str, Any]], list[tuple[str, list[Any]]]],
) -> list[tuple[str, list[Any]]]:
    """Keep generic spin selectors separate from purchase identity/level.

    purchased_feature and purchased_feature_level are already represented by
    PURCHASE_* modes. Treating them as global additionalSpinOptions creates a
    false Cartesian product (for example applying a feature level to SPIN and
    to unrelated purchases) and double-counts the same semantic path.
    """
    return [
        (field, values)
        for field, values in original(profile)
        if str(field) not in RESERVED_PURCHASE_OPTION_FIELDS
    ]


def _remember_profile(profile: Any) -> Any:
    features = getattr(profile, "purchase_features", None)
    _LOCAL.client_purchase_features = {
        str(item) for item in features if str(item)
    } if isinstance(features, list) else set()
    return profile


def _discover_profile_policy(*args, **kwargs):
    return _remember_profile(_ORIGINAL_DISCOVER_PROFILE(*args, **kwargs))


def _purchase_is_client_proven(name: str, client_features: set[str]) -> bool:
    return any(
        _runtime.purchase_names_equivalent(name, observed)
        or _runtime.purchase_names_equivalent(observed, name)
        for observed in client_features
    )


def _discover_purchase_modes_policy(data: dict[str, Any]) -> list[dict[str, Any]]:
    advertised = _ORIGINAL_DISCOVER_PURCHASE_MODES(data)
    client_features = getattr(_LOCAL, "client_purchase_features", None)
    if not isinstance(client_features, set):
        # No profile context means this is not the execution-stage call. Preserve
        # the original discovery behavior rather than silently deleting modes.
        return advertised
    return [
        mode
        for mode in advertised
        if _purchase_is_client_proven(str(mode.get("name") or ""), client_features)
    ]


def begin_policy_run() -> None:
    _LOCAL.client_purchase_features = None
    _LOCAL.coverage_active = True
    _LOCAL.hyperhive_raw_init = None
    _LOCAL.hyperhive_wager_plan = None


def end_policy_run() -> None:
    _LOCAL.client_purchase_features = None
    _LOCAL.coverage_active = False
    _LOCAL.hyperhive_raw_init = None
    _LOCAL.hyperhive_wager_plan = None


def install_policy(exhaustive_module: Any) -> None:
    """Install provider-local hooks without changing neutral scheduler/core code."""
    global _INSTALLED
    if _INSTALLED:
        return

    _execution.discover_profile = _discover_profile_policy
    _execution.discover_purchase_modes = _discover_purchase_modes_policy
    _execution.resolve_base_bet = resolve_coverage_bet
    _hyperhive._rpc = _hyperhive_rpc_policy

    original_choice_domains = exhaustive_module._choice_domains

    def _scoped(profile: dict[str, Any]) -> list[tuple[str, list[Any]]]:
        return scoped_choice_domains(profile, original_choice_domains)

    exhaustive_module._choice_domains = _scoped
    _INSTALLED = True


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _root_or_first(root: Path, name: str) -> Path | None:
    direct = root / name
    if direct.is_file():
        return direct
    try:
        return next(path for path in root.rglob(name) if path.is_file())
    except StopIteration:
        return None


def _mode_id(mode: dict[str, Any]) -> str:
    name = str(mode.get("name") or "").upper()
    level = mode.get("level")
    return (
        f"PURCHASE_{name}"
        if level is None
        else f"PURCHASE_{name}_LEVEL_{str(level).upper()}"
    )


def _append_unproven_advertised_purchases(
    result: GameTestResult,
    init_data: dict[str, Any],
    profile: dict[str, Any],
) -> list[str]:
    advertised = _runtime.discover_purchase_modes(init_data)
    client_raw = profile.get("purchase_features")
    client_features = {
        str(item) for item in client_raw if str(item)
    } if isinstance(client_raw, list) else set()

    existing = {
        str(item.get("id") or "")
        for item in result.discovered_modes
        if isinstance(item, dict)
    }
    unresolved: list[str] = []
    for mode in advertised:
        name = str(mode.get("name") or "")
        mode_id = _mode_id(mode)
        if not name or mode_id in existing:
            continue
        if _purchase_is_client_proven(name, client_features):
            continue
        result.discovered_modes.append(
            {
                "id": mode_id,
                "kind": "DISCOVERED_ONLY",
                "observed": True,
                "client_observed": False,
                "executable": False,
                "coverage_required": False,
                "evidence_level": "SERVER_ADVERTISED",
                "execution_state": "WIRE_UNPROVEN",
                "discovery_state": "ADVERTISED_ONLY",
                "wire_command": "spin",
                "purchased_feature": name,
                "purchased_feature_level": mode.get("level"),
                "feature_multiplier": mode.get("feature_multiplier"),
                "base_multiplier": mode.get("base_multiplier"),
                "cost_multiplier": mode.get("cost_multiplier"),
                "source": "init.feature_multipliers;wire-unproven",
                "required_options": [mode_id],
                "covered_options": [],
            }
        )
        existing.add(mode_id)
        unresolved.append(mode_id)
    return unresolved


def _estimated_purchase_costs(
    init_data: dict[str, Any],
    coverage_bet: float | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for mode in _runtime.discover_purchase_modes(init_data):
        multiplier = mode.get("cost_multiplier")
        estimated = (
            float(coverage_bet) * float(multiplier)
            if coverage_bet is not None
            and isinstance(multiplier, (int, float))
            and float(multiplier) > 0
            else None
        )
        out.append(
            {
                "mode_id": _mode_id(mode),
                "purchased_feature": mode.get("name"),
                "purchased_feature_level": mode.get("level"),
                "cost_multiplier": multiplier,
                "estimated_debit_at_coverage_bet": estimated,
            }
        )
    return out


def _restore_hyperhive_init_evidence(root: Path) -> None:
    raw = getattr(_LOCAL, "hyperhive_raw_init", None)
    if not isinstance(raw, dict):
        return
    init_path = _root_or_first(root, "init-response.json")
    if init_path is None:
        return
    safe_raw = _hyperhive._safe_json(raw)
    init_path.write_text(
        json.dumps(safe_raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_path_catalog(root: Path, result: GameTestResult) -> None:
    paths: list[dict[str, Any]] = []
    for mode in result.discovered_modes:
        if not isinstance(mode, dict):
            continue
        mode_id = str(mode.get("id") or "")
        attempts = [
            attempt
            for attempt in result.attempts
            if str(attempt.mode_id or "").split("__", 1)[0] == mode_id
        ]
        paths.append(
            {
                "id": mode_id,
                "kind": str(mode.get("kind") or ""),
                "branch_signature": str(mode.get("branch_signature") or ""),
                "executable": mode.get("executable"),
                "coverage_required": bool(mode.get("coverage_required")),
                "required_options": list(mode.get("required_options") or []),
                "covered_options": list(mode.get("covered_options") or []),
                "attempts_observed": len(attempts),
                "terminal_successes": sum(
                    1 for attempt in attempts if attempt.ok and attempt.terminal
                ),
                "wire_steps": sum(int(attempt.wire_steps or 0) for attempt in attempts),
                "artifacts": [
                    str(attempt.artifact_dir)
                    for attempt in attempts
                    if str(attempt.artifact_dir or "")
                ],
            }
        )

    payload = {
        "schema": "tester-spin/bgaming-path-catalog/v1",
        "phase": "coverage",
        "provider": result.provider,
        "game": {
            "slug": result.slug,
            "name": result.game_name,
            "symbol": result.symbol,
        },
        "semantics": {
            "path_completeness": (
                "Every provider/client-demonstrated executable branch must be traversed."
            ),
            "rng_completeness": (
                "Observed random outcomes are samples, not proof that every RNG outcome exists or was exhausted."
            ),
            "future_sampling_unit": (
                "Repeat each executable path independently; do not multiply unrelated modes by a scoped selector."
            ),
        },
        "paths": paths,
    }
    (root / "path-catalog.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def finalize_policy_artifacts(result: GameTestResult) -> GameTestResult:
    root = Path(str(result.run_dir or ""))
    if not root.is_dir():
        return result

    # HyperHive executes with a lowered copy of config.default_bet, but evidence
    # must remain byte-semantically faithful to the provider response.
    _restore_hyperhive_init_evidence(root)

    init_path = _root_or_first(root, "init-response.json")
    profile_path = _root_or_first(root, "profile.json")
    init_data = _read_json(init_path) if init_path is not None else {}
    profile = _read_json(profile_path) if profile_path is not None else {}

    unresolved: list[str] = []
    if init_data:
        unresolved = _append_unproven_advertised_purchases(
            result,
            init_data,
            profile,
        )
    if unresolved:
        if result.status == "OK":
            result.status = "PARCIAL"
        message = (
            "BGaming compras anunciadas sin wire cliente demostrado: "
            + ", ".join(unresolved[:20])
            + "."
        )
        if message not in str(result.error or ""):
            result.error = (str(result.error or "").strip() + " " + message).strip()

    plans: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(root.rglob("init-response.json")):
        data = _read_json(path)
        if not data:
            continue
        plan = wager_plan_from_init(data)
        key = json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = path.name
        entry = plan.to_dict()
        entry["evidence"] = rel
        entry["purchase_costs"] = _estimated_purchase_costs(
            data,
            plan.coverage_bet,
        )
        plans.append(entry)

    if plans:
        catalog = {
            "schema": WAGER_CATALOG_SCHEMA,
            "policy": {
                "coverage": (
                    "Use the minimum provider-advertised wager for exhaustive "
                    "protocol/path traversal; never invent a lower wager."
                ),
                "sampling": (
                    "Preserve default and full wager domain so future statistical "
                    "sampling can be scheduled independently per path."
                ),
                "purchase_identity": (
                    "purchased_feature and purchased_feature_level are scoped to "
                    "PURCHASE_* modes and are not global spin-option dimensions."
                ),
            },
            "plans": plans,
        }
        (root / "wager-catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    _write_path_catalog(root, result)

    # Persist the post-policy result so emulation/export tooling sees the same
    # discovered-mode set returned to the scheduler.
    (root / "result.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


__all__ = [
    "RESERVED_PURCHASE_OPTION_FIELDS",
    "WagerPlan",
    "begin_policy_run",
    "end_policy_run",
    "finalize_policy_artifacts",
    "install_policy",
    "prepare_hyperhive_init_for_coverage",
    "resolve_coverage_bet",
    "scoped_choice_domains",
    "wager_plan_from_init",
]
