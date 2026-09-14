from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any


_GAME_CONTROLLER_CALL = re.compile(
    r"(?:\bthis\s*\.\s*)?\bgameController\s*\.\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
    re.I,
)


def extract_client_action_evidence(source: str) -> dict[str, Any]:
    """Extract neutral method-call evidence from the official D1 client source.

    Method names are reported exactly as structural evidence.  No method is
    promoted to a semantic action (purchase, gamble, choice, etc.) here.
    """

    counts: Counter[str] = Counter()
    for match in _GAME_CONTROLLER_CALL.finditer(source or ""):
        counts[str(match.group(1))] += 1
    return {
        "game_controller_methods": sorted(counts),
        "method_call_counts": dict(sorted(counts.items())),
    }


def build_client_action_evidence(
    script_sources: list[tuple[str, str]],
) -> dict[str, Any]:
    aggregate: Counter[str] = Counter()
    scripts: list[dict[str, Any]] = []

    for url, source in script_sources:
        text = str(source or "")
        evidence = extract_client_action_evidence(text)
        counts = evidence["method_call_counts"]
        for method, count in counts.items():
            aggregate[str(method)] += int(count)
        scripts.append(
            {
                "url": str(url),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "game_controller_methods": evidence["game_controller_methods"],
                "method_call_counts": counts,
            }
        )

    return {
        "schema": "tester-spin/1spin4win-client-action-evidence/v1",
        "scripts": scripts,
        "aggregate": {
            "game_controller_methods": sorted(aggregate),
            "method_call_counts": dict(sorted(aggregate.items())),
        },
    }


__all__ = ["build_client_action_evidence", "extract_client_action_evidence"]
