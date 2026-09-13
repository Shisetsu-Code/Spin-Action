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
from tester_spin.providers.bgaming.server_guided import (
    dynamic_action_option_fields,
    dynamic_action_source,
    dynamic_action_variants,
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
    option_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
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
        if self.option_payloads:
            payload["option_payloads"] = {
                label: dict(options)
                for label, options in self.option_payloads.items()
            }
        if self.selected and self.selected in self.option_costs:
            payload["expected_debit"] = self.option_costs[self.selected]
        if self.selected and self.selected in self.option_payloads:
            payload["selected_options"] = dict(self.option_payloads[self.selected])
        return payload


@dataclass(slots=True)
class _ChoiceRun:
    forced_scope: str = ""
    forced_command: str = ""
    forced_path: tuple[str, ...] = ()
    prompts: list[FlowChoicePrompt] = field(default_factory=list)
    round_paths: dict[tuple[str, str, str], list[str]] = field(default_factory=dict)
    pending: FlowChoicePrompt | None = None
    candidates: list[FlowChoicePrompt] = field(default_factory=list)
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


def _prompt_context(data: dict[str, Any], command: str) -> tuple[str, str, tuple[str, ...]] | None:
    flow = data.get("flow") if isinstance(data, dict) else None
    if not isinstance(flow, dict):
        return None
    scope = flow_choice_scope(data)
    round_id = _clean(flow.get("round_id")) or "<unknown-round>"
    run = _current_run()
    prefix: tuple[str, ...] = ()
    if run is not None:
        prefix = tuple(run.round_paths.get((scope, round_id, command), []))
    return scope, round_id, prefix


def _static_prompt_for(
    data: dict[str, Any],
    command: str,
    available: list[str],
) -> FlowChoicePrompt | None:
    contract = command_contract(command)
    choice = contract.choice if contract is not None else None
    context = _prompt_context(data, command)
    if contract is None or choice is None or not available or context is None:
        return None
    scope, round_id, prefix = context
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


def _dynamic_prompt_for(data: dict[str, Any], command: str) -> FlowChoicePrompt | None:
    variants = dynamic_action_variants(data, command)
    context = _prompt_context(data, command)
    if not variants or context is None:
        return None
    scope, round_id, prefix = context
    payloads = {
        str(item.get("label") or ""): dict(item.get("options") or {})
        for item in variants
        if isinstance(item, dict)
        and str(item.get("label") or "")
        and isinstance(item.get("options"), dict)
    }
    if not payloads:
        return None

    if set(payloads) == {"__execute__"} and "__execute__" in prefix:
        return None

    fields = dynamic_action_option_fields(command)
    option_field = fields[0] if len(fields) == 1 else "<options>"
    return FlowChoicePrompt(
        scope=scope,
        round_id=round_id,
        command=command,
        option_field=option_field,
        source=dynamic_action_source(command),
        prefix=prefix,
        available=tuple(payloads),
        option_payloads=payloads,
    )


def _candidate_prompts(data: dict[str, Any]) -> list[FlowChoicePrompt]:
    flow = data.get("flow") if isinstance(data, dict) else None
    if not isinstance(flow, dict):
        return []
    actions = flow.get("available_actions")
    if not isinstance(actions, list):
        return []

    prompts: list[FlowChoicePrompt] = []
    for raw_action in actions:
        action = _clean(raw_action)
        if action in {"", "init", "spin"}:
            continue

        contract = command_contract(action)
        if contract is not None and contract.choice is not None:
            values = flow_choice_options(data, action)
            prompt = _static_prompt_for(data, action, values) if values else None
        else:
            prompt = _dynamic_prompt_for(data, action)
        if prompt is not None:
            prompts.append(prompt)
    return prompts


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


def _prompts_for(data: dict[str, Any]) -> list[FlowChoicePrompt]:
    run = _current_run()
    prompts = _candidate_prompts(data)
    if prompts:
        if run is not None:
            run.candidates = prompts
        return prompts
    if run is None:
        return []
    cached = [
        prompt for prompt in run.candidates
        if _cached_prompt_matches(data, prompt)
    ]
    run.candidates = cached
    return cached


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
    command = _ORIGINAL_FLOW_CONTINUATION(data)
    run = _current_run()
    prompts = _prompts_for(data)

    if run is not None:
        for prompt in prompts:
            if _forced_choice_needed(run, prompt):
                run.pending = prompt
                return prompt.command

    if command:
        return command

    if run is None or not prompts:
        return ""
    run.pending = prompts[0]
    return prompts[0].command


def _same_prompt(left: FlowChoicePrompt | None, right: FlowChoicePrompt) -> bool:
    return bool(
        left is not None
        and left.scope == right.scope
        and left.round_id == right.round_id
        and left.command == right.command
        and left.prefix == right.prefix
        and left.available == right.available
        and left.option_payloads == right.option_payloads
    )


def _trace_has_prompt(run: _ChoiceRun, prompt: FlowChoicePrompt) -> bool:
    return any(_same_prompt(existing, prompt) for existing in run.prompts)


def _pending_flow_actions_with_choices(data: dict[str, Any]) -> list[str]:
    pending = list(_ORIGINAL_PENDING_FLOW_ACTIONS(data))
    run = _current_run()
    if run is None:
        return pending

    prompts = _candidate_prompts(data)
    run.candidates = prompts
    if not prompts:
        return pending

    handled_commands = {prompt.command for prompt in prompts}
    pending = [item for item in pending if str(item) not in handled_commands]

    for prompt in prompts:
        if not _same_prompt(run.pending, prompt) and not _trace_has_prompt(run, prompt):
            run.prompts.append(prompt)
    return pending


def _merge_payload_options(
    base: dict[str, Any],
    selected_payload: dict[str, Any],
    *,
    command: str,
) -> dict[str, Any]:
    merged = dict(base)
    for field, value in selected_payload.items():
        if field in merged and merged[field] != value:
            raise ValueError(
                f"BGaming {command}: options.{field}={merged[field]!r} "
                f"contradice la rama client-proven {value!r}."
            )
        merged[field] = value
    return merged


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
    if prompt.option_payloads:
        selected_payload = prompt.option_payloads.get(selected)
        if not isinstance(selected_payload, dict):
            raise ValueError(
                f"BGaming {command}: rama {selected!r} sin payload client-proven."
            )
        payload_options = _merge_payload_options(
            payload_options,
            selected_payload,
            command=command,
        )
    else:
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
            kwargs["expected_debit"] = float(queue.pop(0))
            kwargs["allow_observed_debit"] = False
    return _ORIGINAL_VALIDATE_SPIN(data, **kwargs)


def install_flow_choice_adapter() -> None:
    """Install one generic bridge for finite in-round choices.

    Static provider contracts and dynamically client-proven literal payloads use
    the same replay graph. Server advertisement is still required at dispatch.
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
