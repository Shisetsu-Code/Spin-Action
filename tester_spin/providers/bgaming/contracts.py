from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ChoiceContract:
    """Finite user choice proven by provider client/runtime evidence.

    ``container_path`` points to the runtime collection that advertises the legal
    options.  No game title, slug or transient id participates in the contract.
    """

    option_field: str
    container_path: tuple[str, ...]
    value_field: str = "name"


@dataclass(frozen=True, slots=True)
class CommandContract:
    """Provider-level wire contract for a continuation command.

    ``states`` are compatibility guards, not dispatch rules: the command still
    has to be advertised by ``flow.available_actions`` before it can execute.
    Parameterless commands can be dispatched directly; choice commands require a
    finite runtime domain before the choice adapter will expose them.
    """

    command: str
    states: tuple[str, ...] = ()
    choice: ChoiceContract | None = None
    source: str = "provider-api-v2-observed"

    @property
    def parameterless(self) -> bool:
        return self.choice is None

    def accepts_state(self, state: str) -> bool:
        return not self.states or str(state or "") in self.states


# One normalized source of provider protocol knowledge.  These are observed
# BGaming API-v2 wire shapes, never per-title allowlists.  New contracts belong
# here only after HAR/client/runtime evidence demonstrates their payload shape.
_COMMANDS = (
    CommandContract("freespin", states=("freespins",)),
    CommandContract("respin", states=("respin",)),
    CommandContract("play_bonus", states=("play_bonus",)),
    CommandContract("preselection_game", states=("preselection_game",)),
    CommandContract("play_preselection_game", states=("preselection_game",)),
    CommandContract("close", states=("gamble",)),
    CommandContract(
        "select_bonus",
        states=("select_bonus",),
        choice=ChoiceContract(
            option_field="name",
            container_path=("game", "freespin_params", "variants"),
            value_field="name",
        ),
        source="provider-client.bonusChoice+runtime.game.freespin_params.variants",
    ),
)

COMMAND_CONTRACTS: dict[str, CommandContract] = {
    contract.command: contract for contract in _COMMANDS
}


def command_contract(command: str) -> CommandContract | None:
    return COMMAND_CONTRACTS.get(str(command or ""))


def _path_value(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def choice_values(data: dict[str, Any], command: str) -> list[str]:
    """Return the finite legal choice domain for a proven choice command."""
    contract = command_contract(command)
    choice = contract.choice if contract is not None else None
    if choice is None or not isinstance(data, dict):
        return []

    raw = _path_value(data, choice.container_path)
    values: list[str] = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = list(raw.values())
    else:
        return []

    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(choice.value_field)
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            continue
        text = str(value).strip()
        if text and text not in values:
            values.append(text)
    return values


# Compatibility exports used by runtime.py.  They are derived from the command
# registry instead of maintaining a second hand-written vocabulary/state table.
SAFE_CONTINUATION_COMMANDS = frozenset(
    contract.command for contract in _COMMANDS if contract.parameterless
)

CONTINUATION_BY_STATE: dict[str, str] = {}
for contract in _COMMANDS:
    if not contract.parameterless:
        continue
    for state in contract.states:
        if state != contract.command:
            CONTINUATION_BY_STATE[state] = contract.command


__all__ = [
    "COMMAND_CONTRACTS",
    "CONTINUATION_BY_STATE",
    "SAFE_CONTINUATION_COMMANDS",
    "ChoiceContract",
    "CommandContract",
    "choice_values",
    "command_contract",
]
