from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from tester_spin.providers.bgaming import execution as _execution
from tester_spin.providers.bgaming import runtime as _runtime


_LOCAL = threading.local()
_ORIGINAL_FLOW_CONTINUATION = _runtime.flow_continuation_command
_ORIGINAL_PENDING_FLOW_ACTIONS = _runtime.pending_flow_actions
_ORIGINAL_POST_COMMAND = _runtime.post_command


@dataclass(slots=True)
class BonusChoicePrompt:
    scope: str
    round_id: str
    prefix: tuple[str, ...]
    available: tuple[str, ...]
    selected: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "round_id": self.round_id,
            "prefix": list(self.prefix),
            "available": list(self.available),
            "selected": self.selected,
            "path_after": [*self.prefix, self.selected] if self.selected else list(self.prefix),
            "wire_command": "select_bonus",
            "option_field": "name",
            "source": "runtime.game.freespin_params.variants+provider-client.bonusChoice",
        }


@dataclass(slots=True)
class _ChoiceRun:
    forced_scope: str = ""
    forced_path: tuple[str, ...] = ()
    prompts: list[BonusChoicePrompt] = field(default_factory=list)
    round_paths: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    pending: BonusChoicePrompt | None = None


def _clean_name(value: Any) -> str:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip()
    return ""


def _variant_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            name = _clean_name(item.get("name"))
            if name and name not in names:
                names.append(name)
        return names

    if isinstance(value, dict):
        for key, item in value.items():
            name = ""
            if isinstance(item, dict):
                name = _clean_name(item.get("name"))
            if not name:
                name = _clean_name(key)
            if name and name not in names:
                names.append(name)
    return names


def bonus_choice_options(data: dict[str, Any]) -> list[str]:
    """Return server-advertised free-spin bonus choices with an observed wire contract.

    BGaming's loaded client converts ``game.freespin_params.variants`` to a map by
    ``variant.name`` and submits the chosen name with:

        command=select_bonus, options={name: <variant>}

    The command is executable only when runtime flow also advertises
    ``select_bonus``. We do not derive choices from labels, translations,
    paytables or title names.
    """
    if not isinstance(data, dict):
        return []
    flow = data.get("flow")
    if not isinstance(flow, dict):
        return []
    state = str(flow.get("state") or "").strip()
    actions_raw = flow.get("available_actions")
    actions = {str(item) for item in actions_raw} if isinstance(actions_raw, list) else set()
    if state != "select_bonus" and "select_bonus" not in actions:
        return []

    game = data.get("game")
    if not isinstance(game, dict):
        return []
    params = game.get("freespin_params")
    if not isinstance(params, dict):
        return []
    return _variant_names(params.get("variants"))


def bonus_choice_scope(data: dict[str, Any]) -> str:
    flow = data.get("flow") if isinstance(data, dict) else None
    if not isinstance(flow, dict):
        return "SPIN"
    purchased = flow.get("purchased_feature")
    if not isinstance(purchased, dict):
        return "SPIN"
    name = _clean_name(purchased.get("name"))
    if not name:
        return "SPIN"
    scope = "PURCHASE_" + name.upper()
    level = _clean_name(purchased.get("level"))
    if level:
        scope += "_LEVEL_" + level.upper()
    return scope


def begin_bonus_choice_run(
    *,
    forced_scope: str = "",
    forced_path: tuple[str, ...] | list[str] = (),
) -> None:
    _LOCAL.run = _ChoiceRun(
        forced_scope=str(forced_scope or ""),
        forced_path=tuple(str(item) for item in forced_path if str(item)),
    )


def end_bonus_choice_run() -> list[dict[str, Any]]:
    run = getattr(_LOCAL, "run", None)
    _LOCAL.run = None
    if not isinstance(run, _ChoiceRun):
        return []
    return [prompt.to_dict() for prompt in run.prompts]


def _current_run() -> _ChoiceRun | None:
    run = getattr(_LOCAL, "run", None)
    return run if isinstance(run, _ChoiceRun) else None


def _prompt_for(data: dict[str, Any]) -> BonusChoicePrompt | None:
    available = bonus_choice_options(data)
    if not available:
        return None
    flow = data.get("flow")
    if not isinstance(flow, dict):
        return None
    scope = bonus_choice_scope(data)
    round_id = _clean_name(flow.get("round_id")) or "<unknown-round>"
    run = _current_run()
    prefix: tuple[str, ...] = ()
    if run is not None:
        prefix = tuple(run.round_paths.get((scope, round_id), []))
    return BonusChoicePrompt(
        scope=scope,
        round_id=round_id,
        prefix=prefix,
        available=tuple(available),
    )


def _flow_continuation_with_bonus_choice(data: dict[str, Any]) -> str:
    command = _ORIGINAL_FLOW_CONTINUATION(data)
    if command:
        return command
    prompt = _prompt_for(data)
    if prompt is None:
        return ""
    run = _current_run()
    if run is not None:
        run.pending = prompt
    return "select_bonus"


def _pending_flow_actions_with_bonus_choice(data: dict[str, Any]) -> list[str]:
    pending = list(_ORIGINAL_PENDING_FLOW_ACTIONS(data))
    if bonus_choice_options(data):
        pending = [item for item in pending if str(item) != "select_bonus"]
    return pending


def _post_command_with_bonus_choice(
    runtime,
    command: str,
    *,
    timeout_s: float,
    options: dict[str, Any] | None = None,
    extra_data: dict[str, Any] | None = None,
):
    if command != "select_bonus":
        return _ORIGINAL_POST_COMMAND(
            runtime,
            command,
            timeout_s=timeout_s,
            options=options,
            extra_data=extra_data,
        )

    run = _current_run()
    prompt = run.pending if run is not None else None
    if prompt is None or not prompt.available:
        raise ValueError(
            "BGaming select_bonus anunciado sin dominio autoritativo en "
            "game.freespin_params.variants."
        )

    depth = len(prompt.prefix)
    selected = prompt.available[0]
    if (
        run is not None
        and run.forced_scope == prompt.scope
        and depth < len(run.forced_path)
    ):
        forced = run.forced_path[depth]
        if forced not in prompt.available:
            raise ValueError(
                f"BGaming select_bonus: variante forzada {forced!r} no está en "
                f"{list(prompt.available)!r} para {prompt.scope}."
            )
        selected = forced

    payload_options = dict(options) if isinstance(options, dict) else {}
    existing = _clean_name(payload_options.get("name"))
    if existing and existing != selected:
        raise ValueError(
            f"BGaming select_bonus: options.name={existing!r} contradice "
            f"la rama seleccionada {selected!r}."
        )
    payload_options["name"] = selected

    result = _ORIGINAL_POST_COMMAND(
        runtime,
        command,
        timeout_s=timeout_s,
        options=payload_options,
        extra_data=extra_data,
    )

    prompt.selected = selected
    if run is not None:
        run.prompts.append(prompt)
        run.round_paths[(prompt.scope, prompt.round_id)] = [*prompt.prefix, selected]
        run.pending = None
    return result


def install_bonus_choice_adapter() -> None:
    """Install the evidence-backed parameterized continuation into API-v2 execution."""
    # execution.py imported these functions by name, while validate_spin() looks
    # them up in runtime.py. Patch both namespaces so execution and validation
    # agree that an evidenced select_bonus state is a handled continuation.
    for module in (_runtime, _execution):
        if getattr(module.flow_continuation_command, "__name__", "") != "_flow_continuation_with_bonus_choice":
            module.flow_continuation_command = _flow_continuation_with_bonus_choice
        if getattr(module.pending_flow_actions, "__name__", "") != "_pending_flow_actions_with_bonus_choice":
            module.pending_flow_actions = _pending_flow_actions_with_bonus_choice
    if getattr(_execution.post_command, "__name__", "") != "_post_command_with_bonus_choice":
        _execution.post_command = _post_command_with_bonus_choice


__all__ = [
    "begin_bonus_choice_run",
    "bonus_choice_options",
    "bonus_choice_scope",
    "end_bonus_choice_run",
    "install_bonus_choice_adapter",
]
