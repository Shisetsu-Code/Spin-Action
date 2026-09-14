from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tester_spin.models import GameTestResult


def _terminal_proof(attempt: Any) -> bool:
    return bool(
        attempt.ok
        and attempt.terminal
        and not str(attempt.error or "").strip()
        and not str(attempt.warning or "").strip()
    )


def successful_wire_commands(result: GameTestResult) -> dict[str, int]:
    """Count action/command values only from successful terminal attempt artifacts."""
    run_dir = str(result.run_dir or "").strip()
    if not run_dir:
        return {}
    root = Path(run_dir).resolve()
    if not root.is_dir():
        return {}

    counts: dict[str, int] = {}
    for attempt in result.attempts:
        if not _terminal_proof(attempt):
            continue
        raw_dir = str(attempt.artifact_dir or "").strip()
        if not raw_dir:
            continue
        directory = Path(raw_dir).resolve()
        if not directory.is_relative_to(root) or not directory.is_dir():
            continue

        for path in sorted(directory.rglob("*request.json")):
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    continue
                payload = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            value = payload.get("action", payload.get("command"))
            if value is None or isinstance(value, bool):
                continue
            command = str(value).strip()
            if command:
                counts[command] = counts.get(command, 0) + 1
    return counts
