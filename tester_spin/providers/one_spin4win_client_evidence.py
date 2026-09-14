from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from typing import Any


_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
_GAME_CONTROLLER_CALL = re.compile(
    rf"(?:\bthis\s*\.\s*)?\bgameController\s*\.\s*({_IDENTIFIER})\s*\(",
    re.I,
)
_GAME_CONTROLLER_DOT_REFERENCE = re.compile(
    rf"(?:\bthis\s*\.\s*)?\bgameController\s*\.\s*({_IDENTIFIER})",
    re.I,
)
_GAME_CONTROLLER_BRACKET_REFERENCE = re.compile(
    rf"(?:\bthis\s*\.\s*)?\bgameController\s*\[\s*['\"]({_IDENTIFIER})['\"]\s*\]",
    re.I,
)
_CONTROLLER_PROTOTYPE_REFERENCE = re.compile(
    rf"\b({_IDENTIFIER}Controller)\s*\.\s*prototype\s*(?:\.\s*({_IDENTIFIER})|\[\s*['\"]({_IDENTIFIER})['\"]\s*\])",
    re.I,
)
_JS_META_REFERENCES = frozenset({"prototype"})


def extract_client_action_evidence(source: str) -> dict[str, Any]:
    """Extract neutral controller evidence from an official D1 client source.

    Names are reported exactly as structural evidence. No method/reference is
    promoted to a semantic action (purchase, gamble, choice, etc.) here.
    """

    text = source or ""
    call_counts: Counter[str] = Counter()
    for match in _GAME_CONTROLLER_CALL.finditer(text):
        call_counts[str(match.group(1))] += 1

    references: set[str] = set()
    references.update(
        str(match.group(1))
        for match in _GAME_CONTROLLER_DOT_REFERENCE.finditer(text)
        if str(match.group(1)).casefold() not in _JS_META_REFERENCES
    )
    references.update(
        str(match.group(1))
        for match in _GAME_CONTROLLER_BRACKET_REFERENCE.finditer(text)
        if str(match.group(1)).casefold() not in _JS_META_REFERENCES
    )

    prototype_methods: dict[str, set[str]] = defaultdict(set)
    for match in _CONTROLLER_PROTOTYPE_REFERENCE.finditer(text):
        controller = str(match.group(1))
        method = str(match.group(2) or match.group(3) or "")
        if controller and method:
            prototype_methods[controller].add(method)

    return {
        "game_controller_methods": sorted(call_counts),
        "method_call_counts": dict(sorted(call_counts.items())),
        "game_controller_references": sorted(references),
        "controller_prototype_methods": {
            controller: sorted(methods)
            for controller, methods in sorted(prototype_methods.items())
        },
    }


def build_client_action_evidence(
    script_sources: list[tuple[str, str]],
) -> dict[str, Any]:
    aggregate_calls: Counter[str] = Counter()
    aggregate_references: set[str] = set()
    aggregate_prototypes: dict[str, set[str]] = defaultdict(set)
    scripts: list[dict[str, Any]] = []

    for url, source in script_sources:
        text = str(source or "")
        evidence = extract_client_action_evidence(text)
        counts = evidence["method_call_counts"]
        for method, count in counts.items():
            aggregate_calls[str(method)] += int(count)
        aggregate_references.update(
            str(method) for method in evidence["game_controller_references"]
        )
        for controller, methods in evidence["controller_prototype_methods"].items():
            aggregate_prototypes[str(controller)].update(str(method) for method in methods)

        scripts.append(
            {
                "url": str(url),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "game_controller_methods": evidence["game_controller_methods"],
                "method_call_counts": counts,
                "game_controller_references": evidence["game_controller_references"],
                "controller_prototype_methods": evidence["controller_prototype_methods"],
            }
        )

    return {
        "schema": "tester-spin/1spin4win-client-action-evidence/v1",
        "scripts": scripts,
        "aggregate": {
            "game_controller_methods": sorted(aggregate_calls),
            "method_call_counts": dict(sorted(aggregate_calls.items())),
            "game_controller_references": sorted(aggregate_references),
            "controller_prototype_methods": {
                controller: sorted(methods)
                for controller, methods in sorted(aggregate_prototypes.items())
            },
        },
    }


__all__ = ["build_client_action_evidence", "extract_client_action_evidence"]
