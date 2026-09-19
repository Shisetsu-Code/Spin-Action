from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import requests

from tester_spin.providers.bgaming import execution as _execution
from tester_spin.providers.bgaming import runtime as _runtime
from tester_spin.providers.bgaming.contracts import (
    choice_costs,
    choice_values,
    command_contract,
)
from tester_spin.providers.bgaming.dynamic_index_domains import (
    ACCEPTED,
    PROTOCOL_ERROR,
    SEMANTIC_REJECTION,
    TRANSPORT_ERROR,
)
from tester_spin.providers.bgaming.server_guided import (
    dynamic_action_option_fields,
    dynamic_action_source,
    dynamic_action_unresolved_variants,
    dynamic_action_unresolved_variants_any_state,
    dynamic_action_variants,
    dynamic_action_variants_any_state,
)


_LOCAL = threading.local()
_RESOLVED_LOCAL = threading.local()
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
    sequence_specs: dict[str, dict[str, Any]] = field(default_factory=dict)
    unresolved_option_variants: list[dict[str, Any]] = field(default_factory=list)
    selected: str = ""
    sequence_completed: bool = False
    sequence_picks: list[int] = field(default_factory=list)

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
        if self.sequence_specs:
            payload["sequence_specs"] = {
                label: dict(spec)
                for label, spec in self.sequence_specs.items()
                if str(label) and isinstance(spec, dict)
            }
        if self.unresolved_option_variants:
            payload["unresolved_option_variants"] = [
                dict(item)
                for item in self.unresolved_option_variants
                if isinstance(item, dict)
            ]
        if self.selected and self.selected in self.option_costs:
            payload["expected_debit"] = self.option_costs[self.selected]
        if self.selected and self.selected in self.option_payloads:
            payload["selected_options"] = dict(self.option_payloads[self.selected])
        if self.selected and self.selected in self.sequence_specs:
            payload["sequence_completed"] = bool(self.sequence_completed)
            payload["sequence_picks"] = list(self.sequence_picks)
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
    dynamic_probe: dict[str, Any] = field(default_factory=dict)
    probe_result: dict[str, Any] = field(default_factory=dict)
    active_sequence: dict[str, Any] = field(default_factory=dict)


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
    dynamic_probe: dict[str, Any] | None = None,
) -> None:
    _LOCAL.run = _ChoiceRun(
        forced_scope=str(forced_scope or ""),
        forced_command=str(forced_command or ""),
        forced_path=tuple(str(item) for item in forced_path if str(item)),
        dynamic_probe=dict(dynamic_probe) if isinstance(dynamic_probe, dict) else {},
    )


def flow_choice_probe_result() -> dict[str, Any]:
    run = _current_run()
    return dict(run.probe_result) if run is not None else {}


def end_flow_choice_run() -> list[dict[str, Any]]:
    run = getattr(_LOCAL, "run", None)
    _LOCAL.run = None
    if not isinstance(run, _ChoiceRun):
        return []
    return [prompt.to_dict() for prompt in run.prompts]


def _current_run() -> _ChoiceRun | None:
    run = getattr(_LOCAL, "run", None)
    return run if isinstance(run, _ChoiceRun) else None


def begin_resolved_dynamic_choice_run() -> None:
    _RESOLVED_LOCAL.variants = {}


def end_resolved_dynamic_choice_run() -> None:
    _RESOLVED_LOCAL.variants = {}


def _resolved_registry() -> dict[
    tuple[str, str, tuple[str, ...]],
    dict[str, dict[str, Any]],
]:
    value = getattr(_RESOLVED_LOCAL, "variants", None)
    if not isinstance(value, dict):
        value = {}
        _RESOLVED_LOCAL.variants = value
    return value


def register_resolved_dynamic_choice(
    *,
    scope: str,
    command: str,
    prefix: tuple[str, ...] | list[str],
    label: str,
    options: dict[str, Any],
    source: str,
) -> None:
    clean_scope = str(scope or "")
    clean_command = str(command or "")
    clean_prefix = tuple(str(item) for item in prefix if str(item))
    clean_label = str(label or "")
    if not clean_scope or not clean_command or not clean_label:
        raise ValueError("BGaming resolved dynamic choice requires scope/command/label.")
    key = (clean_scope, clean_command, clean_prefix)
    _resolved_registry().setdefault(key, {})[clean_label] = {
        "options": dict(options),
        "source": str(source or "dynamic-index-boundary-proof"),
    }


def _resolved_dynamic_choices(
    scope: str,
    command: str,
    prefix: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    rows = _resolved_registry().get((scope, command, tuple(prefix)), {})
    return {
        str(label): dict(item)
        for label, item in rows.items()
        if str(label) and isinstance(item, dict)
    }


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


def _server_sequence_progress(
    data: dict[str, Any],
    command: str,
) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    flow = data.get("flow")
    if not isinstance(flow, dict):
        return None
    if _clean(flow.get("state")).casefold() != str(command or "").casefold():
        return None
    actions = flow.get("available_actions")
    advertised = {
        _clean(item).casefold()
        for item in actions
    } if isinstance(actions, list) else set()
    if str(command or "").casefold() not in advertised:
        return None

    features = data.get("features")
    cards = features.get("cards_data") if isinstance(features, dict) else None
    if not isinstance(cards, dict):
        return None
    raw_issued = cards.get("issued")
    if isinstance(raw_issued, bool):
        return None
    try:
        issued = int(raw_issued)
    except (TypeError, ValueError):
        return None
    if issued <= 0:
        return None

    raw_list = cards.get("list")
    if not isinstance(raw_list, list):
        return None
    selected: list[int] = []
    for item in raw_list:
        if not isinstance(item, dict):
            return None
        raw_index = item.get("index")
        if isinstance(raw_index, bool):
            return None
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return None
        if index < 0:
            return None
        if index not in selected:
            selected.append(index)
    if len(selected) > issued:
        return None

    return {
        "issued": issued,
        "selected_indices": selected,
        "authority": "features.cards_data.issued+list",
    }


def _sequence_label(literal_options: dict[str, Any], field: str) -> str:
    parts = [
        f"{key}={__import__('json').dumps(literal_options[key], ensure_ascii=False, sort_keys=True)}"
        for key in sorted(literal_options)
    ]
    parts.append(f"{field}=<server-sequence>")
    return "|".join(parts)


def _setup_variant_exists(
    variants: list[dict[str, Any]],
    command: str,
) -> bool:
    expected_mode = f"select_{str(command or '').strip().lower()}"
    for item in variants:
        if not isinstance(item, dict):
            continue
        options = item.get("options")
        if not isinstance(options, dict):
            continue
        if str(options.get("mode") or "").strip().lower() == expected_mode:
            return True
    return False


def _sequence_specs_from_unresolved(
    data: dict[str, Any],
    command: str,
    unresolved_variants: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    progress = _server_sequence_progress(data, command)
    if progress is None:
        return {}, list(unresolved_variants)

    specs: dict[str, dict[str, Any]] = {}
    remaining: list[dict[str, Any]] = []
    for variant in unresolved_variants:
        if not isinstance(variant, dict):
            continue
        fields = [
            str(item)
            for item in variant.get("unresolved_fields") or []
            if str(item)
        ]
        literal = dict(variant.get("literal_options") or {})
        if fields != ["index"]:
            remaining.append(dict(variant))
            continue
        label = _sequence_label(literal, "index")
        specs[label] = {
            "field": "index",
            "literal_options": literal,
            "authority": str(progress["authority"]),
            "issued": int(progress["issued"]),
            "selected_indices": list(progress["selected_indices"]),
        }
    return specs, remaining


def _dynamic_prompt_for(data: dict[str, Any], command: str) -> FlowChoicePrompt | None:
    variants = dynamic_action_variants(data, command)
    unresolved_variants = dynamic_action_unresolved_variants(data, command)

    # The first response that enters a server-issued picker can arrive before
    # server-guided evidence for that exact flow.state has been remembered.
    # In that narrow case, cards_data.issued+list is independent provider
    # authority that we are inside the picker, so reuse the already client-proven
    # command serializer shape from an earlier state instead of falling back to
    # blind index probing.
    sequence_progress = _server_sequence_progress(data, command)
    if sequence_progress is not None:
        if not variants:
            variants = dynamic_action_variants_any_state(command)
        if not unresolved_variants:
            unresolved_variants = dynamic_action_unresolved_variants_any_state(
                command
            )

    context = _prompt_context(data, command)
    if context is None or (not variants and not unresolved_variants):
        return None
    scope, round_id, prefix = context

    flow = data.get("flow") if isinstance(data, dict) else None
    state = _clean(flow.get("state")) if isinstance(flow, dict) else ""
    # A forwarded manual index call is a client action inside the picker, not a
    # separate root choice. When the provider advertises a client-proven setup
    # action such as mode=select_pick_cards, defer that unresolved call until
    # the server actually enters the picker state.
    if state.casefold() != str(command or "").casefold() and _setup_variant_exists(
        variants,
        command,
    ):
        unresolved_variants = [
            dict(item)
            for item in unresolved_variants
            if [
                str(field)
                for field in item.get("unresolved_fields") or []
                if str(field)
            ] != ["index"]
        ]

    sequence_specs, unresolved_variants = _sequence_specs_from_unresolved(
        data,
        command,
        unresolved_variants,
    )

    # Dynamically learned literal payloads come from distinct client call sites.
    # Re-sending the same literal payload from the same round without any new
    # client/runtime evidence can stall forever. Treat each literal variant as
    # single-use per command/round path.
    already_used = set(prefix)
    payloads = {
        str(item.get("label") or ""): dict(item.get("options") or {})
        for item in variants
        if isinstance(item, dict)
        and str(item.get("label") or "")
        and str(item.get("label") or "") not in already_used
        and isinstance(item.get("options"), dict)
    }
    for label, item in _resolved_dynamic_choices(scope, command, prefix).items():
        if label in already_used:
            continue
        options = item.get("options")
        if isinstance(options, dict):
            payloads[label] = dict(options)

    sequence_specs = {
        label: spec
        for label, spec in sequence_specs.items()
        if label not in already_used
    }
    available = tuple([*payloads.keys(), *sequence_specs.keys()])
    if not available and not unresolved_variants:
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
        available=available,
        option_payloads=payloads,
        sequence_specs=sequence_specs,
        unresolved_option_variants=unresolved_variants,
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
        and not prompt.option_payloads
    ]
    # Dynamic prompts are intentionally not resurrected from cache after their
    # literal variants are exhausted. Reusing one would recreate the exact loop
    # the single-use rule is designed to prevent.
    run.candidates = cached
    return cached


def _matching_dynamic_probe(
    run: _ChoiceRun,
    prompt: FlowChoicePrompt,
) -> dict[str, Any] | None:
    probe = run.dynamic_probe
    if not isinstance(probe, dict) or not probe or run.probe_result:
        return None
    if str(probe.get("scope") or "") != prompt.scope:
        return None
    if str(probe.get("command") or "") != prompt.command:
        return None
    prefix = tuple(str(value) for value in probe.get("prefix") or [])
    if prefix != prompt.prefix:
        return None
    field = str(probe.get("field") or "")
    raw_value = probe.get("value")
    if not field or isinstance(raw_value, bool):
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    literal_options = probe.get("literal_options")
    literal_options = dict(literal_options) if isinstance(literal_options, dict) else {}

    for variant in prompt.unresolved_option_variants:
        if not isinstance(variant, dict):
            continue
        unresolved_fields = [
            str(item)
            for item in variant.get("unresolved_fields") or []
            if str(item)
        ]
        if unresolved_fields != [field]:
            continue
        if dict(variant.get("literal_options") or {}) != literal_options:
            continue
        return {
            "scope": prompt.scope,
            "command": prompt.command,
            "prefix": list(prompt.prefix),
            "literal_options": literal_options,
            "field": field,
            "value": value,
            "source": str(variant.get("source") or prompt.source),
        }
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
    command = _ORIGINAL_FLOW_CONTINUATION(data)
    run = _current_run()

    if run is not None and run.probe_result:
        return ""

    if run is not None and run.active_sequence:
        sequence_command = str(run.active_sequence.get("command") or "")
        progress = _server_sequence_progress(data, sequence_command)
        if progress is not None and len(progress["selected_indices"]) < progress["issued"]:
            run.active_sequence["progress"] = progress
            return sequence_command
        run.active_sequence = {}

    prompts = _prompts_for(data)

    if run is not None:
        for prompt in prompts:
            if _matching_dynamic_probe(run, prompt) is not None:
                run.pending = prompt
                return prompt.command
        for prompt in prompts:
            if _forced_choice_needed(run, prompt):
                run.pending = prompt
                return prompt.command

    if command:
        return command

    if run is None:
        return ""
    executable = [prompt for prompt in prompts if prompt.available]
    if not executable:
        return ""
    run.pending = executable[0]
    return executable[0].command


def _same_prompt(left: FlowChoicePrompt | None, right: FlowChoicePrompt) -> bool:
    return bool(
        left is not None
        and left.scope == right.scope
        and left.round_id == right.round_id
        and left.command == right.command
        and left.prefix == right.prefix
        and left.available == right.available
        and left.option_payloads == right.option_payloads
        and left.sequence_specs == right.sequence_specs
    )


def _trace_has_prompt(run: _ChoiceRun, prompt: FlowChoicePrompt) -> bool:
    return any(_same_prompt(existing, prompt) for existing in run.prompts)


def _pending_flow_actions_with_choices(data: dict[str, Any]) -> list[str]:
    pending = list(_ORIGINAL_PENDING_FLOW_ACTIONS(data))
    run = _current_run()
    if run is None:
        return pending

    if run.active_sequence:
        sequence_command = str(run.active_sequence.get("command") or "")
        progress = _server_sequence_progress(data, sequence_command)
        if progress is not None and len(progress["selected_indices"]) < progress["issued"]:
            return [item for item in pending if str(item) != sequence_command]
        run.active_sequence = {}

    prompts = _candidate_prompts(data)
    run.candidates = prompts
    if not prompts:
        return pending

    handled_commands = {
        prompt.command
        for prompt in prompts
        if prompt.available or _matching_dynamic_probe(run, prompt) is not None
    }
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


def _next_sequence_index(progress: dict[str, Any]) -> int:
    used = {
        int(value)
        for value in progress.get("selected_indices") or []
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    }
    candidate = 0
    while candidate in used:
        candidate += 1
    return candidate


def _response_selected_indices(data: dict[str, Any]) -> list[int]:
    features = data.get("features") if isinstance(data, dict) else None
    cards = features.get("cards_data") if isinstance(features, dict) else None
    raw_list = cards.get("list") if isinstance(cards, dict) else None
    if not isinstance(raw_list, list):
        return []
    selected: list[int] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        raw_index = item.get("index")
        if isinstance(raw_index, bool):
            continue
        try:
            value = int(raw_index)
        except (TypeError, ValueError):
            continue
        if value >= 0 and value not in selected:
            selected.append(value)
    return selected


def _update_sequence_after_response(
    run: _ChoiceRun,
    prompt: FlowChoicePrompt,
    command: str,
    data: dict[str, Any],
) -> None:
    progress = _server_sequence_progress(data, command)
    if progress is None:
        observed = _response_selected_indices(data)
        if observed:
            prompt.sequence_picks = observed
        prompt.sequence_completed = True
        run.active_sequence = {}
        return
    prompt.sequence_picks = list(progress["selected_indices"])
    if len(progress["selected_indices"]) >= int(progress["issued"]):
        prompt.sequence_completed = True
        run.active_sequence = {}
        return
    run.active_sequence["progress"] = progress


def _dispatch_sequence(
    run: _ChoiceRun,
    prompt: FlowChoicePrompt,
    selected: str,
    runtime,
    command: str,
    *,
    timeout_s: float,
    options: dict[str, Any] | None,
    extra_data: dict[str, Any] | None,
):
    spec = prompt.sequence_specs.get(selected)
    if not isinstance(spec, dict):
        raise ValueError(f"BGaming {command}: secuencia {selected!r} sin contrato.")

    progress = run.active_sequence.get("progress")
    if not isinstance(progress, dict):
        progress = {
            "issued": int(spec.get("issued") or 0),
            "selected_indices": list(spec.get("selected_indices") or []),
            "authority": str(spec.get("authority") or ""),
        }
    issued = int(progress.get("issued") or 0)
    if issued <= 0:
        raise ValueError(f"BGaming {command}: secuencia sin issued positivo.")

    payload_options = dict(options) if isinstance(options, dict) else {}
    literal = dict(spec.get("literal_options") or {})
    payload_options = _merge_payload_options(
        payload_options,
        literal,
        command=command,
    )
    field = str(spec.get("field") or "index")
    payload_options[field] = _next_sequence_index(progress)

    result = _ORIGINAL_POST_COMMAND(
        runtime,
        command,
        timeout_s=timeout_s,
        options=payload_options,
        extra_data=extra_data,
    )
    data = result[2] if len(result) > 2 and isinstance(result[2], dict) else {}

    if not run.active_sequence:
        prompt.selected = selected
        prompt.sequence_picks = list(progress.get("selected_indices") or [])
        run.prompts.append(prompt)
        run.round_paths[(prompt.scope, prompt.round_id, prompt.command)] = [
            *prompt.prefix,
            selected,
        ]
        run.active_sequence = {
            "scope": prompt.scope,
            "round_id": prompt.round_id,
            "command": command,
            "label": selected,
            "prompt": prompt,
            "progress": progress,
        }

    _update_sequence_after_response(run, prompt, command, data)
    run.pending = None
    return result


def _post_command_with_choices(
    runtime,
    command: str,
    *,
    timeout_s: float,
    options: dict[str, Any] | None = None,
    extra_data: dict[str, Any] | None = None,
):
    run = _current_run()
    if run is not None and run.active_sequence:
        active_command = str(run.active_sequence.get("command") or "")
        active_prompt = run.active_sequence.get("prompt")
        active_label = str(run.active_sequence.get("label") or "")
        if (
            active_command == command
            and isinstance(active_prompt, FlowChoicePrompt)
            and active_label
        ):
            return _dispatch_sequence(
                run,
                active_prompt,
                active_label,
                runtime,
                command,
                timeout_s=timeout_s,
                options=options,
                extra_data=extra_data,
            )

    prompt = run.pending if run is not None else None
    if prompt is None or prompt.command != command:
        return _ORIGINAL_POST_COMMAND(
            runtime,
            command,
            timeout_s=timeout_s,
            options=options,
            extra_data=extra_data,
        )

    dynamic_probe = _matching_dynamic_probe(run, prompt)
    if dynamic_probe is not None:
        payload_options = dict(options) if isinstance(options, dict) else {}
        payload_options = _merge_payload_options(
            payload_options,
            dynamic_probe["literal_options"],
            command=command,
        )
        field = str(dynamic_probe["field"])
        value = int(dynamic_probe["value"])
        if field in payload_options and payload_options[field] != value:
            raise ValueError(
                f"BGaming {command}: options.{field}={payload_options[field]!r} "
                f"contradice probe dinámico {value!r}."
            )
        payload_options[field] = value
        base_result = {
            **dynamic_probe,
            "target_reached": True,
            "request_options": dict(payload_options),
        }
        try:
            result = _ORIGINAL_POST_COMMAND(
                runtime,
                command,
                timeout_s=timeout_s,
                options=payload_options,
                extra_data=extra_data,
            )
        except requests.HTTPError as exc:
            status = (
                int(exc.response.status_code)
                if exc.response is not None
                else 0
            )
            if status == 422:
                outcome = SEMANTIC_REJECTION
            elif status in {408, 425, 429} or status >= 500:
                outcome = TRANSPORT_ERROR
            else:
                outcome = PROTOCOL_ERROR
            run.probe_result = {
                **base_result,
                "outcome": outcome,
                "http_status": status,
                "error": f"{type(exc).__name__}: {exc}",
                "error_evidence": _runtime.http_error_evidence(exc.response),
            }
            run.pending = None
            raise
        except requests.RequestException as exc:
            run.probe_result = {
                **base_result,
                "outcome": TRANSPORT_ERROR,
                "http_status": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
            run.pending = None
            raise
        except Exception as exc:
            run.probe_result = {
                **base_result,
                "outcome": PROTOCOL_ERROR,
                "http_status": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
            run.pending = None
            raise

        data = result[2] if len(result) > 2 and isinstance(result[2], dict) else {}
        provider_errors = _runtime.provider_error_envelope(data)
        run.probe_result = {
            **base_result,
            "outcome": (
                SEMANTIC_REJECTION
                if provider_errors is not None
                else ACCEPTED
            ),
            "http_status": int(getattr(result[0], "status_code", 200) or 200),
            "provider_errors": provider_errors,
            "response_state": (
                str((data.get("flow") or {}).get("state") or "")
                if isinstance(data.get("flow"), dict)
                else ""
            ),
        }
        run.pending = None
        return result

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

    if selected in prompt.sequence_specs:
        return _dispatch_sequence(
            run,
            prompt,
            selected,
            runtime,
            command,
            timeout_s=timeout_s,
            options=options,
            extra_data=extra_data,
        )

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
    "begin_resolved_dynamic_choice_run",
    "end_flow_choice_run",
    "end_resolved_dynamic_choice_run",
    "flow_choice_probe_result",
    "flow_choice_options",
    "flow_choice_scope",
    "install_flow_choice_adapter",
    "register_resolved_dynamic_choice",
]
