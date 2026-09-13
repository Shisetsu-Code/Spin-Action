from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA = "tester-spin/game-structure/v1"
EVIDENCE_KINDS = {"UI_OBSERVED", "WIRE_OBSERVED", "HAR_OBSERVED", "CLIENT_PROVEN", "SERVER_ADVERTISED", "DERIVED_VALIDATED", "HEURISTIC"}


@dataclass(frozen=True)
class State:
    server_state: str
    available_actions: tuple[str, ...] = ()
    terminal: bool | None = None
    # Only provider-proven structural distinctions; never balance or RNG values.
    signature: str = ""


@dataclass(frozen=True)
class Choice:
    label: str
    options: dict[str, Any]
    source: str = "CLIENT_PROVEN"


@dataclass(frozen=True)
class Observation:
    before: State
    command: str
    request: dict[str, Any]
    response: Any = None
    after: State | None = None
    executed: bool = False
    outcome_observed: bool = False
    validation: str = "not_validated"
    context_before: dict[str, Any] = field(default_factory=dict)
    context_after: dict[str, Any] = field(default_factory=dict)
    prefix: tuple[str, ...] = ()
    evidence_kind: str = "WIRE_OBSERVED"
    choice_fields: tuple[str, ...] = ()
