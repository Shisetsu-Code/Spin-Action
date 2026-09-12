from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.rubyplay.adapter import RubyPlayProvider as _RubyPlayProvider


INDEX_BRANCH_ACTIONS = {"select", "pick"}


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _observed_index_actions(result: GameTestResult) -> dict[str, set[int]]:
    found: dict[str, set[int]] = {}
    root = Path(str(result.run_dir or ""))
    if not root.is_dir():
        return found
    for path in root.rglob("*request.json"):
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        action = str(payload.get("action") or "").strip().lower()
        if action not in INDEX_BRANCH_ACTIONS:
            continue
        index = payload.get("index")
        if isinstance(index, bool):
            continue
        try:
            parsed = int(index)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            found.setdefault(action, set()).add(parsed)
    return found


def apply_rubyplay_path_audit(
    result: GameTestResult,
    *,
    progress: Progress,
) -> GameTestResult:
    """Refuse OK when RubyPlay reached an indexed choice without a finite domain.

    The HAR/client contract proves that ``select`` and ``pick`` carry an ``index``.
    Current runtime evidence does not prove the complete set of legal indexes, so
    choosing index 0 (or the sequential pick cursor) validates transport but not
    exhaustive branch coverage. We preserve the observed indexes and leave the
    domain explicitly unresolved instead of guessing an upper bound.
    """
    if result.status in {"ERROR", "CANCELADO"} or not result.run_dir:
        return result

    observed = _observed_index_actions(result)
    if not observed:
        return result

    for action, indexes in sorted(observed.items()):
        result.discovered_modes.append(
            {
                "id": f"RUBYPLAY_{action.upper()}_INDEX_DOMAIN",
                "kind": "INDEXED_CHOICE",
                "observed": True,
                "executable": True,
                "wire_command": action,
                "observed_indices": sorted(indexes),
                "coverage_required": True,
                "branch_signature": f"RUBYPLAY:{action}:index-domain",
                "required_options": ["DOMAIN_UNRESOLVED"],
                "covered_options": [],
                "reason": (
                    "el cliente demuestra que la acción usa index, pero los HAR/"
                    "contratos actuales no demuestran el dominio completo; no se "
                    "puede considerar exhaustiva una elección arbitraria"
                ),
            }
        )

    if result.status == "OK":
        result.status = "PARCIAL"
    detail = ", ".join(
        f"{action} indexes observados={sorted(indexes)}"
        for action, indexes in sorted(observed.items())
    )
    message = (
        "RubyPlay cobertura indexada pendiente: " + detail
        + "; falta dominio finito demostrado por provider."
    )
    if message not in str(result.error or ""):
        result.error = (str(result.error or "").strip() + " " + message).strip()
    progress(message)

    try:
        Path(result.run_dir, "result.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return result


class RubyPlayProvider(_RubyPlayProvider):
    """RubyPlay adapter with fail-closed exhaustive branch semantics."""

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        result = super().test_game(
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )
        return apply_rubyplay_path_audit(result, progress=progress)


__all__ = ["RubyPlayProvider", "apply_rubyplay_path_audit"]
