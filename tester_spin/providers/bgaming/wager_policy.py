from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tester_spin.providers.bgaming import runtime as _runtime


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
    values: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            continue
        number = float(item)
        if number > 0 and number not in values:
            values.append(number)
    return sorted(values)


def wager_plan_from_init(data: dict[str, Any]) -> WagerPlan:
    if not isinstance(data, dict):
        return WagerPlan(None, None, [], None, "unknown")

    options = data.get("options")
    if isinstance(options, dict):
        raw_default = options.get("default_bet")
        default_bet = float(raw_default) if isinstance(raw_default, (int, float)) and not isinstance(raw_default, bool) and raw_default > 0 else None
        available = _positive_numbers(options.get("available_bets"))
        source = "options.available_bets:min" if available else ""
        if not available:
            available = _positive_numbers(options.get("line_bets"))
            source = "options.line_bets:min" if available else source
        if not available and default_bet is not None:
            available = [default_bet]
            source = "options.default_bet"
        coverage_bet = min(available) if available else default_bet
        balance = _runtime.balance_total(data)
        return WagerPlan(default_bet, coverage_bet, available, float(balance) if isinstance(balance, (int, float)) else None, source or "unknown")

    result = data.get("result")
    config = result.get("config") if isinstance(result, dict) else None
    if isinstance(config, dict):
        raw_default = config.get("default_bet")
        default_bet = float(raw_default) if isinstance(raw_default, (int, float)) and not isinstance(raw_default, bool) and raw_default > 0 else None
        available = _positive_numbers(config.get("bet_limits"))
        if not available and default_bet is not None:
            available = [default_bet]
        coverage_bet = min(available) if available else default_bet
        raw_balance = result.get("balance")
        balance = float(raw_balance) if isinstance(raw_balance, (int, float)) and not isinstance(raw_balance, bool) else None
        return WagerPlan(default_bet, coverage_bet, available, balance, "result.config.bet_limits:min" if available else "result.config.default_bet")

    return WagerPlan(None, None, [], None, "unknown")


def resolve_coverage_bet(data: dict[str, Any], fallback) -> tuple[int | float | None, str]:
    plan = wager_plan_from_init(data)
    if plan.coverage_bet is None:
        return fallback(data)
    value = plan.coverage_bet
    return (int(value) if value.is_integer() else value), plan.source
