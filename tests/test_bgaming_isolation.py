from __future__ import annotations

import ast
import unittest
from pathlib import Path


class BGamingIsolationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
