from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from tester_spin.providers.bgaming import execution as _execution
from tester_spin.providers.bgaming import runtime as _runtime
from tester_spin.providers.bgaming.contracts import (
    choice_costs,
    choice_values,
    command_contract,
)


_LOCAL = threading.local()
_ORIGINAL_FLOW_CONTINUATION = _runtime.flow_continuation_command
_ORIGINAL_PENDING_FLOW_ACTIONS = _runtime.pending_flow_actions
_ORIGINAL_POST_COMMAND = _runtime.post_command
_ORIGINAL_VALIDATE_SPIN = _execution.validate_spin


@dataclass(slots=True)
class FlowChoicePrompt:
    scope: str
    round_id: str
    command: str
    option_field: str
    source: str
    prefix: tuple[str, ...]
    available: tuple[str, ...]
    option_costs: dict[str, float] = field(default_factory=dict)
    selected: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "scope": self.scope,
            "round_id": self.round_id,
            "command": self.command,
            "prefix": list(self.prefix),
            "available": list(self.available),
            "selected": self.selected,
            "path_after": [*self.prefix, self.selected]
            if self.selected
            else list(self.prefix),
            "wire_command": self.command,
            "option_field": self.option_field,
            "source": self.source,
        }
        if self.option_costs:
            payload["option_costs"] = dict(self.option_costs)
        if self.selected and self.selected in self.option_costs:
            payload["expected_debit"] = self.option_costs[self.selected]
        return payload


@dataclass(slots=True)
class _ChoiceRun:
    forced_scope: str = ""
    forced_command: str = ""
    forced_path: tuple[str, ...] = ()
    prompts: list[FlowChoicePrompt] = field(default_factory=list)
    round_paths: dict[tuple[str, str, str], list[str]] = field(default_factory=dict)
    pending: FlowChoicePrompt | None = None
    candidate: FlowChoicePrompt | None = None
    validation_debits: dict[str, list[float]] = field(default_factory=dict)


def _clean(value: Any) -> str:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip()
    return ""


def flow_choice_scope(data: dict[str, Any]) -> str:
    flow = data.get("flow") if isinstance(data, dict) else None
    if not isinstance(flow, dict):
        return "SPIN"
    purchased = flow.get("purchased_feature")
    if not isinstance(purchased, dict):
        return "SPIN"
    name = _clean(purchased.get("name"))
    if not name:
        return "SPIN"
    scope = "PURCHASE_" + name.upper()
    level = _clean(purchased.get("level"))
    if level:
        scope += "_LEVEL_" + level.upper()
    return scope


def flow_choice_options(data: dict[str, Any], command: str) -> list[str]:
    """Return legal options only when command, state and runtime domain agree."""
    if not isinstance(data, dict):
        return []
    contract = command_contract(command)
    if contract is None or contract.choice is None:
        return []

    flow = data.get("flow")
    if not isinstance(flow, dict):
        return []
    state = _clean(flow.get("state"))
    actions = flow.get("available_actions")
    advertised = {
        _clean(item) for item in actions
    } if isinstance(actions, list) else set()
    if command not in advertised or not contract.accepts_state(state):
        return []
    return choice_values(data, command)


def _choice_candidate(data: dict[str, Any]) -> tuple[str, list[str]] | None:
    flow = data.get("flow") if isinstance(data, dict) else None
    if not isinstance(flow, dict):
        return None
    actions = flow.get("available_actions")
    if not isinstance(actions, list):
        return None

    # Server advertisement is the dispatch authority. The state name never
    # becomes a command by itself (important for state=select_bonus with
    # available action=play_bonus_game).
    for raw_action in actions:
        action = _clean(raw_action)
        if action in {"", "init", "spin"}:
            continue
        contract = command_contract(action)
        if contract is None or contract.choice is None:
            continue
        values = flow_choice_options(data, action)
        if values:
            return action, values
    return None


def begin_flow_choice_run(
    *,
    forced_scope: str = "",
    forced_command: str = "",
    forced_path: tuple[str, ...] | list[str] = (),
) -> None:
    _LOCAL.run = _ChoiceRun(
        forced_scope=str(forced_scope or ""),
        forced_command=str(forced_command or ""),
        forced_path=tuple(str(item) for item in forced_path if str(item)),
    )


def end_flow_choice_run() -> list[dict[str, Any]]:
    run = getattr(_LOCAL, "run", None)
    _LOCAL.run = None
    if not isinstance(run, _ChoiceRun):
        return []
    return [prompt.to_dict() for prompt in run.prompts]


def _current_run() -> _ChoiceRun | None:
    run = getattr(_LOCAL, "run", None)
    return run if isinstance(run, _ChoiceRun) else None


def _prompt_for(
    data: dict[str, Any],
    command: str,
    available: list[str],
) -> FlowChoicePrompt | None:
    contract = command_contract(command)
    choice = contract.choice if contract is not None else None
    if contract is None or choice is None or not available:
        return None
    flow = data.get("flow")
    if not isinstance(flow, dict):
        return None

    scope = flow_choice_scope(data)
    round_id = _clean(flow.get("round_id")) or "<unknown-round>"
    run = _current_run()
    prefix: tuple[str, ...] = ()
    if run is not None:
        prefix = tuple(
            run.round_paths.get((scope, round_id, command), [])
        )
    costs = choice_costs(data, command)
    return FlowChoicePrompt(
        scope=scope,
        round_id=round_id,
        command=command,
        option_field=choice.option_field,
        source=contract.source,
        prefix=prefix,
        available=tuple(available),
        option_costs={
            value: float(costs[value])
            for value in available
            if value in costs
        },
    )


def _cached_prompt_matches(data: dict[str, Any], prompt: FlowChoicePrompt) -> bool:
    flow = data.get("flow") if isinstance(data, dict) else None
    if not isinstance(flow, dict):
        return False
    actions = flow.get("available_actions")
    action_names = {
        _clean(item) for item in actions
    } if isinstance(actions, list) else set()
    round_id = _clean(flow.get("round_id")) or "<unknown-round>"
    return (
        prompt.command in action_names
        and prompt.round_id == round_id
        and prompt.scope == flow_choice_scope(data)
    )


def _candidate_prompt(data: dict[str, Any]) -> FlowChoicePrompt | None:
    run = _current_run()
    candidate = _choice_candidate(data)
    if candidate is not None:
        command, available = candidate
        prompt = _prompt_for(data, command, available)
        if run is not None:
            run.candidate = prompt
        return prompt

    if run is not None and run.candidate is not None:
        if _cached_prompt_matches(data, run.candidate):
            return run.candidate
    return None


def _forced_choice_needed(run: _ChoiceRun, prompt: FlowChoicePrompt) -> bool:
    if run.forced_scope != prompt.scope:
        return False
    if run.forced_command and run.forced_command != prompt.command:
        return False
    depth = len(prompt.prefix)
    if depth >= len(run.forced_path):
        return False
    if tuple(run.forced_path[:depth]) != prompt.prefix:
        return False
    return run.forced_path[depth] in prompt.available


def _flow_continuation_with_choices(data: dict[str, Any]) -> str:
    # A normal parameterless continuation remains the default path. A forced
    # exhaustive replay may override it only for a client-proven finite choice
    # that was observed in the same round. This is what lets buy_extra_bonus be
    # sampled even when freespin is simultaneously available.
    command = _ORIGINAL_FLOW_CONTINUATION(data)
    run = _current_run()
    prompt = _candidate_prompt(data)

    if run is not None and prompt is not None and _forced_choice_needed(run, prompt):
        run.pending = prompt
        return prompt.command

    if command:
        return command

    if run is None or prompt is None:
        return ""
    run.pending = prompt
    return prompt.command


def _same_prompt(left: FlowChoicePrompt | None, right: FlowChoicePrompt) -> bool:
    return bool(
        left is not None
        and left.scope == right.scope
        and left.round_id == right.round_id
        and left.command == right.command
        and left.prefix == right.prefix
        and left.available == right.available
    )


def _pending_flow_actions_with_choices(data: dict[str, Any]) -> list[str]:
    pending = list(_ORIGINAL_PENDING_FLOW_ACTIONS(data))
    run = _current_run()
    candidate = _choice_candidate(data)
    if run is None:
        return pending

    if candidate is None:
        run.candidate = None
        return pending

    command, available = candidate
    prompt = _prompt_for(data, command, available)
    if prompt is None:
        run.candidate = None
        return pending

    run.candidate = prompt
    pending = [item for item in pending if str(item) != command]

    # Preserve the branch point in the trace even when the normal path chooses
    # a simultaneous parameterless continuation (for example freespin). The
    # exhaustive wrapper can then replay this round and force each legal option.
    if not _same_prompt(run.pending, prompt):
        run.prompts.append(prompt)
    return pending


def _post_command_with_choices(
    runtime,
    command: str,
    *,
    timeout_s: float,
    options: dict[str, Any] | None = None,
    extra_data: dict[str, Any] | None = None,
):
    run = _current_run()
    prompt = run.pending if run is not None else None
    if prompt is None or prompt.command != command:
        return _ORIGINAL_POST_COMMAND(
            runtime,
            command,
            timeout_s=timeout_s,
            options=options,
            extra_data=extra_data,
        )

    if not prompt.available:
        raise ValueError(
            f"BGaming {command}: acción de elección sin dominio finito autoritativo."
        )

    depth = len(prompt.prefix)
    selected = prompt.available[0]
    if (
        run.forced_scope == prompt.scope
        and (not run.forced_command or run.forced_command == prompt.command)
        and depth < len(run.forced_path)
    ):
        forced = run.forced_path[depth]
        if forced not in prompt.available:
            raise ValueError(
                f"BGaming {command}: opción forzada {forced!r} no está en "
                f"{list(prompt.available)!r} para {prompt.scope}."
            )
        selected = forced

    payload_options = dict(options) if isinstance(options, dict) else {}
    existing = _clean(payload_options.get(prompt.option_field))
    if existing and existing != selected:
        raise ValueError(
            f"BGaming {command}: options.{prompt.option_field}={existing!r} "
            f"contradice la rama {selected!r}."
        )
    payload_options[prompt.option_field] = selected

    result = _ORIGINAL_POST_COMMAND(
        runtime,
        command,
        timeout_s=timeout_s,
        options=payload_options,
        extra_data=extra_data,
    )

    prompt.selected = selected
    run.prompts.append(prompt)
    run.round_paths[(prompt.scope, prompt.round_id, prompt.command)] = [
        *prompt.prefix,
        selected,
    ]
    if selected in prompt.option_costs:
        run.validation_debits.setdefault(command, []).append(
            float(prompt.option_costs[selected])
        )
    run.pending = None
    return result


def _validate_spin_with_choices(data: dict[str, Any], **kwargs):
    run = _current_run()
    command = str(kwargs.get("command") or "spin")
    if run is not None:
        queue = run.validation_debits.get(command)
        if isinstance(queue, list) and queue:
            # The official client supplied this price in the response that made
            # the choice available. Validate the resulting balance against that
            # advertised debit instead of treating every continuation as free.
            kwargs["expected_debit"] = float(queue.pop(0))
            kwargs["allow_observed_debit"] = False
    return _ORIGINAL_VALIDATE_SPIN(data, **kwargs)


def install_flow_choice_adapter() -> None:
    """Install one generic bridge for finite in-round choices.

    execution.py imported runtime helpers by name, so both namespaces need the
    same bridge. The bridge does not add commands or state aliases; it only
    supplies options for commands already proven by the contract registry and
    advertised by the current server response.
    """
    for module in (_runtime, _execution):
        if getattr(module.flow_continuation_command, "__name__", "") != "_flow_continuation_with_choices":
            module.flow_continuation_command = _flow_continuation_with_choices
        if getattr(module.pending_flow_actions, "__name__", "") != "_pending_flow_actions_with_choices":
            module.pending_flow_actions = _pending_flow_actions_with_choices
    if getattr(_execution.post_command, "__name__", "") != "_post_command_with_choices":
        _execution.post_command = _post_command_with_choices
    if getattr(_execution.validate_spin, "__name__", "") != "_validate_spin_with_choices":
        _execution.validate_spin = _validate_spin_with_choices


__all__ = [
    "begin_flow_choice_run",
    "end_flow_choice_run",
    "flow_choice_options",
    "flow_choice_scope",
    "install_flow_choice_adapter",
]
