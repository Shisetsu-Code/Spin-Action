from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ChoiceContract:
    """Finite user choice proven by provider client/runtime evidence.

    ``container_path`` points to the runtime collection that advertises the legal
    options. No game title, slug or transient id participates in the contract.

    Most BGaming choices expose a list/dict of objects and select one field from
    each object. Some client-proven commands instead expose a mapping whose keys
    are the legal wire values. ``mapping_keys`` models that provider-level shape
    without hardcoding the concrete option names.

    ``mapping_values_are_debit`` means the mapping value is the authoritative
    price for that option. When ``balance_path`` is available, choices the
    official client would disable for insufficient balance are filtered out.

    repeatable_domain means executing one option can leave the same command
    and finite option domain advertised again. Repetition is runtime behavior,
    not a new structural branch; exhaustive coverage therefore proves each
    option independently rather than enumerating arbitrary option sequences.
    """

    option_field: str
    container_path: tuple[str, ...]
    value_field: str = "name"
    mapping_keys: bool = False
    mapping_values_are_debit: bool = False
    balance_path: tuple[str, ...] = ()
    repeatable_domain: bool = False
    outcomeless_ack_path: tuple[str, ...] = ()


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


# One normalized source of provider protocol knowledge. These are observed
# BGaming API-v2 wire shapes, never per-title allowlists. New contracts belong
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
    CommandContract(
        "buy_extra_bonus",
        choice=ChoiceContract(
            option_field="bonus_type",
            container_path=("features", "bonus_data", "bonus_game_prices"),
            mapping_keys=True,
            mapping_values_are_debit=True,
            balance_path=("balance", "wallet"),
            repeatable_domain=True,
            outcomeless_ack_path=(
                "features",
                "bonus_data",
                "last_purchased_extra_bonus",
            ),
        ),
        source=(
            "provider-client.buyExtraBonus(options.bonus_type)+"
            "runtime.features.bonus_data.bonus_game_prices+balance.wallet"
        ),
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


def _choice_mapping(
    data: dict[str, Any],
    choice: ChoiceContract,
) -> dict[str, Any]:
    raw = _path_value(data, choice.container_path)
    if not choice.mapping_keys or not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if str(key).strip()}


def _mapping_value_allowed(
    data: dict[str, Any],
    choice: ChoiceContract,
    value: Any,
) -> bool:
    if not choice.mapping_values_are_debit:
        return True
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return False
    if not choice.balance_path:
        return True
    balance = _path_value(data, choice.balance_path)
    if not isinstance(balance, (int, float)) or isinstance(balance, bool):
        return True
    return float(value) <= float(balance)


def choice_values(data: dict[str, Any], command: str) -> list[str]:
    """Return the finite legal choice domain for a proven choice command."""
    contract = command_contract(command)
    choice = contract.choice if contract is not None else None
    if choice is None or not isinstance(data, dict):
        return []

    if choice.mapping_keys:
        values: list[str] = []
        for key, mapping_value in _choice_mapping(data, choice).items():
            text = str(key).strip()
            if (
                text
                and text not in values
                and _mapping_value_allowed(data, choice, mapping_value)
            ):
                values.append(text)
        return values

    raw = _path_value(data, choice.container_path)
    values = []
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


def choice_costs(data: dict[str, Any], command: str) -> dict[str, float]:
    """Return authoritative per-choice debits when the client proves them.

    Commands without a client-proven price mapping return an empty dict. Values
    are filtered with the same legality rules as ``choice_values`` so callers do
    not validate or schedule an option the official client would disable.
    """
    contract = command_contract(command)
    choice = contract.choice if contract is not None else None
    if (
        choice is None
        or not choice.mapping_keys
        or not choice.mapping_values_are_debit
        or not isinstance(data, dict)
    ):
        return {}

    costs: dict[str, float] = {}
    for key, mapping_value in _choice_mapping(data, choice).items():
        if not _mapping_value_allowed(data, choice, mapping_value):
            continue
        costs[str(key)] = float(mapping_value)
    return costs


def choice_outcomeless_acknowledged(
    data: dict[str, Any],
    command: str,
    selected: str,
) -> bool:
    """Validate a provider-declared acknowledgement for a choice without outcome."""
    contract = command_contract(command)
    choice = contract.choice if contract is not None else None
    if choice is None or not choice.outcomeless_ack_path:
        return False
    selected_value = str(selected or "").strip()
    if not selected_value:
        return False
    acknowledged = _path_value(data, choice.outcomeless_ack_path)
    return str(acknowledged or "").strip() == selected_value


# Compatibility exports used by runtime.py. They are derived from the command
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
    "choice_costs",
    "choice_values",
    "command_contract",
]
