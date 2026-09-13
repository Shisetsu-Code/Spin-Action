from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import GameTestResult, SpinAttempt
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
            # Sending a request alone is not evidence of a validated branch.
            self.assertEqual(report["branch_points"][0]["covered"], [])
            self.assertEqual(report["branch_points"][0]["missing"], ["LEFT", "RIGHT", "RANDOM"])

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

    def test_unexecuted_actionable_mode_cannot_hide_behind_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self._result(Path(temp))
            result.discovered_modes.append(
                {
                    "id": "GAMBLE",
                    "kind": "FEATURE",
                    "observed": False,
                    "wire_command": "gamble",
                }
            )

            enforce_complete_path_coverage(result)

            self.assertEqual(result.status, "PARCIAL")
            report = build_path_coverage_report(result)
            self.assertEqual(report["missing_count"], 1)
            self.assertEqual(report["branch_points"][0]["missing"], ["gamble"])

    def test_nonexecutable_purchase_mode_is_required_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self._result(Path(temp))
            result.discovered_modes.append(
                {
                    "id": "PURCHASE_UNKNOWN",
                    "kind": "PURCHASE",
                    "observed": True,
                    "executable": False,
                    "feature_buy": "unknown_buy",
                }
            )

            enforce_complete_path_coverage(result)

            self.assertEqual(result.status, "PARCIAL")
            self.assertIn("unknown_buy", result.error)

    def test_discovered_only_metadata_is_not_forced_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self._result(Path(temp))
            result.discovered_modes.append(
                {
                    "id": "GAME_MODES",
                    "kind": "DISCOVERED_ONLY",
                    "observed": True,
                    "executable": False,
                    "values": ["Normal", "FreeSpins"],
                }
            )

            enforce_complete_path_coverage(result)

            self.assertEqual(result.status, "OK")
            self.assertTrue(build_path_coverage_report(result)["complete"])

    def test_pragmatic_fso_artifact_occurrence_is_tracked_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = root / "PURCHASE_1" / "attempt-00001"
            attempt.mkdir(parents=True)
            for step, selected in ((2, 0), (5, 1)):
                (attempt / f"fso-selection-{step:03d}.json").write_text(
                    json.dumps(
                        {
                            "schema": "tester-spin/pragmatic-fso-selection/v1",
                            "option_indices": [0, 1],
                            "selected_index": selected,
                        }
                    ),
                    encoding="utf-8",
                )
            result = self._result(root)

            enforce_complete_path_coverage(result)

            # A selection at one FSO prompt must not count as covering another.
            result.attempts.append(SpinAttempt(number=1, ok=True, terminal=True, artifact_dir=str(attempt)))
            self.assertEqual(result.status, "PARCIAL")
            report = build_path_coverage_report(result)
            artifact_points = [
                item for item in report["branch_points"]
                if item["source"] == "pragmatic_fso_artifact"
            ]
            self.assertEqual(len(artifact_points), 2)
            self.assertTrue(all(len(item["missing"]) == 1 for item in artifact_points))


if __name__ == "__main__":
    unittest.main()
