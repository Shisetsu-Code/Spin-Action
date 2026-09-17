from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.rubyplay.adapter import RubyPlayProvider as _ExecutionProvider
from tester_spin.providers.rubyplay.choice_probe import (
    choice_domain_mode_id,
    choice_domain_signature,
    expand_rubyplay_index_domains,
    observed_index_prompts,
)
from tester_spin.providers.rubyplay.exhaustive import RubyPlayProvider as _CatalogProvider


def _domain_complete(mode: dict[str, Any]) -> bool:
    required_raw = mode.get("required_options")
    covered_raw = mode.get("covered_options")
    if not isinstance(required_raw, list) or not required_raw:
        return False
    if not isinstance(covered_raw, list):
        return False
    required = {str(value) for value in required_raw}
    covered = {str(value) for value in covered_raw}
    if "DOMAIN_UNRESOLVED" in required:
        return False
    return bool(required and required.issubset(covered))


def _prefix_matches(mode: dict[str, Any], prefix: tuple[str, ...]) -> bool:
    raw = mode.get("prefix")
    if isinstance(raw, list):
        return [str(value) for value in raw] == list(prefix)
    return not prefix


def _proven_domain(
    result: GameTestResult,
    *,
    parent: str,
    action: str,
    prefix: tuple[str, ...],
) -> dict[str, Any] | None:
    mode_id = choice_domain_mode_id(parent, action, prefix)
    candidates = [
        mode
        for mode in result.discovered_modes
        if isinstance(mode, dict)
        and str(mode.get("id") or "") == mode_id
        and str(mode.get("kind") or "").upper() == "INDEXED_CHOICE"
        and str(mode.get("parent") or "") == parent
        and str(mode.get("wire_command") or "").strip().lower() == action
        and _prefix_matches(mode, prefix)
        and _domain_complete(mode)
    ]
    return candidates[0] if len(candidates) == 1 else None


def apply_rubyplay_choice_audit(
    result: GameTestResult,
    *,
    progress: Progress,
) -> GameTestResult:
    """Fail closed unless every observed RubyPlay indexed prompt has proof."""
    if result.status in {"ERROR", "CANCELADO"} or not result.run_dir:
        return result

    observed = observed_index_prompts(result)
    if not observed:
        return result

    # Drop only obsolete global rows from the older audit. Parent/path-scoped
    # proof rows are retained and checked below.
    result.discovered_modes = [
        item
        for item in result.discovered_modes
        if not (
            isinstance(item, dict)
            and str(item.get("kind") or "").upper() == "INDEXED_CHOICE"
            and str(item.get("id") or "").startswith("RUBYPLAY_")
        )
    ]

    unresolved: list[tuple[str, str, tuple[str, ...], set[int]]] = []
    for (parent, action, prefix), indexes in sorted(
        observed.items(),
        key=lambda item: (
            item[0][0],
            item[0][1],
            len(item[0][2]),
            item[0][2],
        ),
    ):
        proven = _proven_domain(
            result,
            parent=parent,
            action=action,
            prefix=prefix,
        )
        if proven is not None:
            continue

        mode_id = choice_domain_mode_id(parent, action, prefix)
        result.discovered_modes = [
            mode
            for mode in result.discovered_modes
            if not (
                isinstance(mode, dict)
                and str(mode.get("id") or "") == mode_id
                and str(mode.get("kind") or "").upper() == "INDEXED_CHOICE"
            )
        ]
        result.discovered_modes.append(
            {
                "id": mode_id,
                "kind": "INDEXED_CHOICE",
                "parent": parent,
                "prefix": list(prefix),
                "observed": True,
                "executable": True,
                "wire_command": action,
                "observed_indices": sorted(indexes),
                "coverage_required": True,
                "branch_signature": choice_domain_signature(parent, action, prefix),
                "required_options": ["DOMAIN_UNRESOLVED"],
                "covered_options": [],
                "reason": (
                    "RubyPlay indexed prompt was observed on this exact path, but "
                    "isolated live replay did not prove its finite domain boundary."
                ),
            }
        )
        unresolved.append((parent, action, prefix, indexes))

    if unresolved:
        if result.status == "OK":
            result.status = "PARCIAL"
        detail = ", ".join(
            f"{parent}/{action}/prefix={list(prefix)} indexes={sorted(indexes)}"
            for parent, action, prefix, indexes in unresolved
        )
        message = (
            "RubyPlay cobertura indexada pendiente: " + detail
            + "; falta dominio finito demostrado por provider."
        )
        if message not in str(result.error or ""):
            result.error = (str(result.error or "").strip() + " " + message).strip()
        progress(message)
    else:
        observed_parents = {parent for parent, _action, _prefix in observed}
        total = sum(
            len(mode.get("required_options") or [])
            for mode in result.discovered_modes
            if isinstance(mode, dict)
            and str(mode.get("kind") or "").upper() == "INDEXED_CHOICE"
            and str(mode.get("parent") or "") in observed_parents
        )
        progress(f"RubyPlay cobertura indexada demostrada: {total}/{total} opciones.")

    try:
        Path(result.run_dir, "result.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
    return result


class RubyPlayProvider(_CatalogProvider):
    """RubyPlay with isolated live proof for select/pick index domains."""

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        # Call the protocol executor directly so the legacy unresolved-domain audit
        # does not run before isolated probes have a chance to prove each path.
        result = _ExecutionProvider.test_game(
            self,
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )
        if result.status not in {"ERROR", "CANCELADO"} and result.run_dir:
            observed = observed_index_prompts(result)
            if observed:
                result = expand_rubyplay_index_domains(
                    self,
                    game,
                    result,
                    observed=observed,
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=progress,
                )
        return apply_rubyplay_choice_audit(result, progress=progress)


RubyPlayProvider.__module__ = "tester_spin.providers.rubyplay.exhaustive"

__all__ = ["RubyPlayProvider", "apply_rubyplay_choice_audit"]
