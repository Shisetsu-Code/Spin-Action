from __future__ import annotations

import ast
import unittest
from pathlib import Path


class BGamingIsolationTests(unittest.TestCase):
    @staticmethod
    def _attribute_path(node: ast.AST) -> str:
        parts: list[str] = []
        current = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))

    @staticmethod
    def _contains_string_literal(node: ast.AST) -> bool:
        return any(
            isinstance(item, ast.Constant)
            and isinstance(item.value, str)
            and bool(item.value)
            for item in ast.walk(node)
        )

    def test_bgaming_never_imports_other_provider_internals(self) -> None:
        root = Path(__file__).resolve().parents[1] / "tester_spin" / "providers" / "bgaming"
        forbidden = (
            "tester_spin.providers.pragmatic",
            "tester_spin.providers.belatra",
            "tester_spin.providers.one_spin4win",
        )
        violations: list[str] = []

        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = str(node.module or "")
                    if module.startswith(forbidden):
                        violations.append(f"{path.name}:{node.lineno} from {module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        module = str(alias.name or "")
                        if module.startswith(forbidden):
                            violations.append(f"{path.name}:{node.lineno} import {module}")

        self.assertEqual(
            violations,
            [],
            "BGaming debe ser autónomo respecto de los otros providers: "
            + "; ".join(violations),
        )

    def test_bgaming_has_no_per_game_literal_routing(self) -> None:
        root = Path(__file__).resolve().parents[1] / "tester_spin" / "providers" / "bgaming"
        protected_attributes = {
            "game.name",
            "game.slug",
            "runtime.identifier",
        }
        violations: list[str] = []

        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                expressions = [node.left, *node.comparators]
                attribute_paths = {
                    self._attribute_path(expr)
                    for expr in expressions
                    if isinstance(expr, ast.Attribute)
                }
                if not (attribute_paths & protected_attributes):
                    continue
                if not any(
                    self._contains_string_literal(expr)
                    for expr in expressions
                ):
                    continue
                violations.append(
                    f"{path.name}:{node.lineno} "
                    + ",".join(
                        sorted(attribute_paths & protected_attributes)
                    )
                )

        self.assertEqual(
            violations,
            [],
            "BGaming no debe enrutar lógica por nombre/slug/identifier literal: "
            + "; ".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
