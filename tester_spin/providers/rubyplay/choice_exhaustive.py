from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.rubyplay.adapter import RubyPlayProvider as _ExecutionProvider
from tester_spin.providers.rubyplay.choice_probe import expand_rubyplay_index_domains
from tester_spin.providers.rubyplay.exhaustive import (
    RubyPlayProvider as _CatalogProvider,
    _observed_index_actions,
)


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


def _proven_domain(
    result: GameTestResult,
    *,
    parent: str,
    action: str,
) -> dict[str, Any] | None:
    mode_id = f"{parent}__{action.upper()}_INDEX_DOMAIN"
    candidates = [
        mode
        for mode in result.discovered_modes
        if isinstance(mode, dict)
        and str(mode.get("id") or "") == mode_id
        and str(mode.get("kind") or "").upper() == "INDEXED_CHOICE"
        and str(mode.get("parent") or "") == parent
        and str(mode.get("wire_command") or "").strip().lower() == action
        and _domain_complete(mode)
    ]
    return candidates[0] if len(candidates) == 1 else None


def apply_rubyplay_choice_audit(
    result: GameTestResult,
    *,
    progress: Progress,
) -> GameTestResult:
    """Fail closed for indexed RubyPlay choices unless a scoped domain is proven."""
    if result.status in {"ERROR", "CANCELADO"} or not result.run_dir:
        return result

    observed = _observed_index_actions(result)
    if not observed:
        return result

    # Drop only the obsolete global rows from the older audit. Parent-scoped
    # proven rows are retained and checked below.
    result.discovered_modes = [
        item
        for item in result.discovered_modes
        if not (
            isinstance(item, dict)
            and str(item.get("kind") or "").upper() == "INDEXED_CHOICE"
            and str(item.get("id") or "").startswith("RUBYPLAY_")
        )
    ]

    unresolved: list[tuple[str, str, set[int]]] = []
    for (parent, action), indexes in sorted(observed.items()):
        proven = _proven_domain(result, parent=parent, action=action)
        if proven is not None:
            continue

        mode_id = f"{parent}__{action.upper()}_INDEX_DOMAIN"
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
                "observed": True,
                "executable": True,
                "wire_command": action,
                "observed_indices": sorted(indexes),
                "coverage_required": True,
                "branch_signature": f"RUBYPLAY:{parent}:{action}:index-domain",
                "required_options": ["DOMAIN_UNRESOLVED"],
                "covered_options": [],
                "reason": (
                    "RubyPlay indexed choice was observed, but isolated live replay "
                    "did not prove a finite domain boundary."
                ),
            }
        )
        unresolved.append((parent, action, indexes))

    if unresolved:
        if result.status == "OK":
            result.status = "PARCIAL"
        detail = ", ".join(
            f"{parent}/{action} indexes observados={sorted(indexes)}"
            for parent, action, indexes in unresolved
        )
        message = (
            "RubyPlay cobertura indexada pendiente: " + detail
            + "; falta dominio finito demostrado por provider."
        )
        if message not in str(result.error or ""):
            result.error = (str(result.error or "").strip() + " " + message).strip()
        progress(message)
    else:
        total = sum(
            len(mode.get("required_options") or [])
            for mode in result.discovered_modes
            if isinstance(mode, dict)
            and str(mode.get("kind") or "").upper() == "INDEXED_CHOICE"
            and str(mode.get("parent") or "") in {parent for parent, _action in observed}
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
        # does not run before isolated probes have a chance to prove the domain.
        result = _ExecutionProvider.test_game(
            self,
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )
        if result.status not in {"ERROR", "CANCELADO"} and result.run_dir:
            observed = _observed_index_actions(result)
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
