from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True, slots=True)
class ResultNode:
    path: str
    spin_mode: str
    game_mode: int | str | None
    has_state: bool | None
    feature_count: int
    keys: tuple[str, ...]


def walk_tree(value: Any, path: str = "$") -> Iterator[tuple[str, Any]]:
    """Walk arbitrary Red Tiger response JSON without assuming game-specific nesting."""
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk_tree(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_tree(child, f"{path}[{index}]")


def _is_structural_state_node(value: dict[str, Any]) -> bool:
    """Recognize provider game-state objects that legitimately omit ``spinMode``.

    Some Red Tiger titles return the normal spin as ``result.game`` with
    ``gameMode`` + ``hasState`` and the actual outcome fields, but no ``spinMode``.
    Requiring ``spinMode`` made those successful HTTP 200 / success=true spins look
    partial even though the provider returned a complete terminal game state.

    Keep the fallback deliberately structural: a lone ``gameMode`` is not enough.
    We require the provider state marker plus at least one outcome-bearing field.
    """
    if "gameMode" not in value or not isinstance(value.get("hasState"), bool):
        return False
    return any(
        key in value
        for key in (
            "win",
            "stake",
            "reelsBuffer",
            "features",
            "winLines",
            "scatters",
            "nearMiss",
        )
    )


def result_nodes(payload: Any) -> list[ResultNode]:
    """Return structurally identifiable game-state nodes at any response depth.

    Prefer the explicit provider ``spinMode`` when present. For titles whose
    normal-spin response omits it, accept the independently observed
    ``gameMode``/``hasState`` state shape instead. An omitted spin mode remains an
    empty string so Tester-Spin records the evidence without inventing semantics.
    """
    nodes: list[ResultNode] = []
    for path, value in walk_tree(payload):
        if not isinstance(value, dict):
            continue

        raw_spin_mode = value.get("spinMode")
        spin_mode = raw_spin_mode.strip() if isinstance(raw_spin_mode, str) else ""
        if not spin_mode and not _is_structural_state_node(value):
            continue

        features = value.get("features")
        nodes.append(
            ResultNode(
                path=path,
                spin_mode=spin_mode,
                game_mode=value.get("gameMode"),
                has_state=(value.get("hasState") if isinstance(value.get("hasState"), bool) else None),
                feature_count=len(features) if isinstance(features, list) else 0,
                keys=tuple(sorted(str(key) for key in value.keys())),
            )
        )
    return nodes


def observed_modes(payload: Any) -> list[str]:
    modes: list[str] = []
    for node in result_nodes(payload):
        if node.spin_mode and node.spin_mode not in modes:
            modes.append(node.spin_mode)
    return modes
