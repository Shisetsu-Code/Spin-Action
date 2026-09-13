"""Backward-compatible names for the removed bonus-specific choice engine.

New code must import :mod:`tester_spin.providers.bgaming.flow_choices`.
"""

from typing import Any

from tester_spin.providers.bgaming.flow_choices import (
    begin_flow_choice_run,
    end_flow_choice_run,
    flow_choice_options,
    flow_choice_scope,
    install_flow_choice_adapter,
)

begin_bonus_choice_run = begin_flow_choice_run
end_bonus_choice_run = end_flow_choice_run
install_bonus_choice_adapter = install_flow_choice_adapter
bonus_choice_scope = flow_choice_scope


def bonus_choice_options(data: dict[str, Any]) -> list[str]:
    return flow_choice_options(data, "select_bonus")


__all__ = [
    "begin_bonus_choice_run",
    "bonus_choice_options",
    "bonus_choice_scope",
    "end_bonus_choice_run",
    "install_bonus_choice_adapter",
]
