from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tester_spin.models import GameTestResult
from tester_spin.purchase_coverage import finalize_purchase_coverage, make_purchase_option


def _buy_options(gs: dict[str, Any]) -> list[str]:
    buy = gs.get("buyBonus")
    if not isinstance(buy, dict):
        return []
    raw = buy.get("buyTotalBetK")
    options: list[str] = []
    if isinstance(raw, dict):
        for key in raw:
            value = str(key).strip()
            if value and value not in options:
                options.append(value)
    elif isinstance(raw, list):
        for index, item in enumerate(raw):
            if isinstance(item, dict):
                candidate = item.get("id")
                if candidate in (None, ""):
                    candidate = item.get("prefix2")
                if candidate in (None, ""):
                    candidate = index
            else:
                candidate = index
            value = str(candidate).strip()
            if value and value not in options:
                options.append(value)
    return options


def _options_from_artifacts(result: GameTestResult) -> list[str]:
    root_raw = str(result.run_dir or "").strip()
    if not root_raw:
        return []
    root = Path(root_raw)
    path = root / "bootstrap" / "enter.response.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    gs = payload.get("gs")
    return _buy_options(gs) if isinstance(gs, dict) else []


def _options_from_modes(result: GameTestResult) -> list[str]:
    values: list[str] = []
    for mode in result.discovered_modes:
        if not isinstance(mode, dict):
            continue
        kind = str(mode.get("kind") or "").upper()
        if kind not in {"PURCHASE", "PURCHASE_BRANCH"} and str(mode.get("id") or "") != "BELATRA_BUY_BONUS":
            continue
        raw = mode.get("required_options")
        if isinstance(raw, list):
            for item in raw:
                value = str(item).strip()
                if value and value not in values:
                    values.append(value)
    return values


def build_belatra_purchase_coverage(result: GameTestResult) -> dict[str, Any]:
    values = _options_from_modes(result)
    for value in _options_from_artifacts(result):
        if value not in values:
            values.append(value)

    options = [
        make_purchase_option(
            f"BELATRA_BUY_BONUS__{value}",
            provider_selector={"advertised_option": value},
            display_name=f"buyBonus option {value}",
            source_kind="belatra-enter-gs.buyBonus.buyTotalBetK",
            source_evidence={"advertised_option": value},
            executable=False,
            wire_contract_state="UNKNOWN",
            execution_state="NOT_ATTEMPTED",
            terminal=False,
            reason=(
                "Belatra advertises this buy option, but the mapping into the buyBonus request field is not proven by authoritative wire evidence."
            ),
        )
        for value in values
    ]

    return finalize_purchase_coverage(
        result,
        options=options,
        inventory_state="COMPLETE" if values else "UNKNOWN",
        authority="belatra-enter-metadata-only",
        no_purchase_proven=False,
        reason=(
            "Belatra buyTotalBetK candidates are preserved, but no purchase is executable until the provider-local buyBonus wire contract is proven."
            if values
            else "Belatra current runtime does not authoritatively prove purchase presence or absence."
        ),
    )


__all__ = ["build_belatra_purchase_coverage"]
