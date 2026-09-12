from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import GameTestResult
from tester_spin.providers.path_coverage import (
    build_path_coverage_report,
    enforce_complete_path_coverage,
)


class PathCoverageTests(unittest.TestCase):
    @staticmethod
    def _result(root: Path) -> GameTestResult:
        return GameTestResult(
            provider="synthetic",
            slug="branching-game",
            game_name="Branching Game",
            game_url="https://example.invalid/game",
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
            run_dir=str(root),
        )

    def test_explicit_uncovered_branch_downgrades_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self._result(root)
            result.discovered_modes.append(
                {
                    "id": "PURCHASE_SUPER",
                    "kind": "CHOICE_BRANCH",
                    "coverage_required": True,
                    "branch_signature": "PURCHASE_SUPER:ROOT",
                    "required_options": ["GOD_A", "GOD_B", "RANDOM"],
                    "covered_options": ["GOD_A"],
                }
            )

            enforce_complete_path_coverage(result)

            self.assertEqual(result.status, "PARCIAL")
            self.assertIn("GOD_B", result.error)
            self.assertIn("RANDOM", result.error)
            report = json.loads((root / "path-coverage.json").read_text(encoding="utf-8"))
            self.assertFalse(report["complete"])
            self.assertEqual(report["missing_count"], 2)

    def test_complete_explicit_branch_stays_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self._result(root)
            result.discovered_modes.append(
                {
                    "id": "PURCHASE_SUPER",
                    "kind": "CHOICE_BRANCH",
                    "coverage_required": True,
                    "branch_signature": "PURCHASE_SUPER:ROOT",
                    "required_options": ["GOD_A", "GOD_B", "RANDOM"],
                    "covered_options": ["GOD_A", "GOD_B", "RANDOM"],
                }
            )

            enforce_complete_path_coverage(result)

            self.assertEqual(result.status, "OK")
            self.assertEqual(result.error, "")
            self.assertTrue(build_path_coverage_report(result)["complete"])

    def test_artifact_scanner_blocks_skipped_choice_for_any_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = root / "PURCHASE_X" / "attempt-00001"
            attempt.mkdir(parents=True)
            (attempt / "response.json").write_text(
                json.dumps(
                    {
                        "success": True,
                        "result": {
                            "game": {
                                "choices": {
                                    "selected": None,
                                    "available": ["LEFT", "RIGHT", "RANDOM"],
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            (attempt / "choice-request.json").write_text(
                json.dumps({"choice": "LEFT"}),
                encoding="utf-8",
            )
            result = self._result(root)

            enforce_complete_path_coverage(result)

            self.assertEqual(result.status, "PARCIAL")
            report = build_path_coverage_report(result)
            self.assertEqual(report["branch_points"][0]["covered"], ["LEFT"])
            self.assertEqual(report["branch_points"][0]["missing"], ["RIGHT", "RANDOM"])

    def test_pragmatic_fso_metadata_is_part_of_global_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self._result(Path(temp))
            result.discovered_modes.append(
                {
                    "id": "PURCHASE_1",
                    "fs_option_indices": [0, 1, 2],
                    "fs_option_selected": [0, 1],
                }
            )

            enforce_complete_path_coverage(result)

            self.assertEqual(result.status, "PARCIAL")
            self.assertIn("2", result.error)


if __name__ == "__main__":
    unittest.main()
