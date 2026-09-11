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


def result_nodes(payload: Any) -> list[ResultNode]:
    """Return structurally identifiable game-state nodes at any response depth.

    A state node is identified by provider fields carried by observed spin results,
    not by a fixed path such as result.game.freeSpins[*]. The same detector therefore
    survives free-spin/respin/bonus nesting and future composite result trees.
    """
    nodes: list[ResultNode] = []
    for path, value in walk_tree(payload):
        if not isinstance(value, dict):
            continue
        spin_mode = value.get("spinMode")
        if not isinstance(spin_mode, str) or not spin_mode.strip():
            continue
        features = value.get("features")
        nodes.append(
            ResultNode(
                path=path,
                spin_mode=spin_mode.strip(),
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
        if node.spin_mode not in modes:
            modes.append(node.spin_mode)
    return modes
