from __future__ import annotations

from copy import deepcopy
from typing import Any


_WAGER_KINDS = {"SPIN", "ANTE_BET", "PURCHASE"}
_CHOICE_KINDS = {
    "CONTINUATION",
    "CHOICE_CONTINUATION",
    "FSO_BRANCH",
    "INDEXED_CHOICE",
}
_DOMAIN_KEYS = ("required_options", "values", "observed_indices", "states")


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _choice_domain(parameters: dict[str, Any]) -> list[Any]:
    for key in _DOMAIN_KEYS:
        values = _as_list(parameters.get(key))
        if values:
            return values
    return []


def _coverage_complete(parameters: dict[str, Any], domain: list[Any]) -> bool | None:
    if not domain:
        return None
    covered = _as_list(parameters.get("covered_options"))
    if not covered:
        return None
    return {str(value) for value in domain}.issubset(
        {str(value) for value in covered}
    )


def build_execution_structure(
    modes: list[dict[str, Any]],
    *,
    provider_domains: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a read-only summary of discovered wagers and selections.

    This is deliberately not a request DSL. It mirrors only stable information
    already present in the provider contract so a later farm program can read the
    discovered betting/choice surface without repeating discovery.
    """

    wagers: list[dict[str, Any]] = []
    choices: list[dict[str, Any]] = []

    for raw_mode in modes:
        if not isinstance(raw_mode, dict):
            continue
        mode_id = str(raw_mode.get("id") or "").strip()
        if not mode_id:
            continue
        kind = str(raw_mode.get("kind") or "UNKNOWN").upper()
        evidence = str(raw_mode.get("evidence") or "")
        executor = str(raw_mode.get("executor") or "")
        parameters = raw_mode.get("options")
        parameters = deepcopy(parameters) if isinstance(parameters, dict) else {}

        if kind in _WAGER_KINDS:
            wager: dict[str, Any] = {
                "mode_id": mode_id,
                "kind": kind,
                "evidence": evidence,
                "executor": executor,
                "parameters": parameters,
            }
            if isinstance(raw_mode.get("cost_multiplier"), (int, float)):
                wager["cost_multiplier"] = raw_mode["cost_multiplier"]
            wagers.append(wager)

        has_choice_domain = any(_as_list(parameters.get(key)) for key in _DOMAIN_KEYS)
        if kind in _CHOICE_KINDS or has_choice_domain:
            domain = _choice_domain(parameters)
            covered = _as_list(parameters.get("covered_options"))
            choice: dict[str, Any] = {
                "mode_id": mode_id,
                "kind": kind,
                "evidence": evidence,
                "executor": executor,
                "domain": domain,
                "covered": covered,
                "coverage_complete": _coverage_complete(parameters, domain),
                "parameters": parameters,
            }
            parent = parameters.get("parent")
            if parent not in (None, ""):
                choice["parent"] = parent
            prefix = parameters.get("prefix")
            if isinstance(prefix, list):
                choice["prefix"] = list(prefix)
            choices.append(choice)

    return {
        "wagers": wagers,
        "choices": choices,
        "provider_domains": deepcopy(provider_domains or {}),
    }


def attach_execution_structure(
    contract: dict[str, Any],
    *,
    provider_domains: dict[str, Any] | None = None,
) -> dict[str, Any]:
    modes = contract.get("modes")
    contract["execution_structure"] = build_execution_structure(
        modes if isinstance(modes, list) else [],
        provider_domains=provider_domains,
    )
    return contract


def select_domains(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return {
        key: deepcopy(source[key])
        for key in keys
        if key in source
        and isinstance(source[key], (str, int, float, bool, list, dict))
    }
