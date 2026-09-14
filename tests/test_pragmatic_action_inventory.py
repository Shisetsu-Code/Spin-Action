from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import GameTestResult
from tester_spin.providers.pragmatic_action_inventory import annotate_pragmatic_action_inventory


def _result(run_dir: str, *, status: str = "OK") -> GameTestResult:
    result = GameTestResult(
        provider="pragmatic",
        slug="demo",
        game_name="Demo",
        game_url="https://example.invalid/demo",
        requested_spins=2,
        successful_spins=2,
        failed_spins=0,
        status=status,
        run_dir=run_dir,
    )
    result.discovered_modes = [
        {"id": "SPIN", "kind": "SPIN", "enabled": True},
        {"id": "PURCHASE_1", "kind": "PURCHASE", "enabled": True},
    ]
    result.structural_map = {"existing": {"preserved": True}}
    return result


def _write_fixture(
    root: Path,
    *,
    catalog_modes: list[dict] | None = None,
    unknown: list | None = None,
    unhandled: list | None = None,
    path_complete: bool = True,
) -> None:
    discovery = root / "discovery"
    discovery.mkdir(parents=True, exist_ok=True)
    (discovery / "doInit.response.json").write_text("{}", encoding="utf-8")
    (discovery / "modes.json").write_text(
        json.dumps(
            {
                "schema": "tester-spin/pragmatic-mode-catalog/v2",
                "raw_sources": {"bls": "", "purInit": "[{bet:200}]", "purInit_e": "1"},
                "modes": catalog_modes
                if catalog_modes is not None
                else [
                    {"id": "SPIN", "kind": "SPIN", "enabled": True},
                    {"id": "PURCHASE_1", "kind": "PURCHASE", "enabled": True},
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "protocol-observations.json").write_text(
        json.dumps(
            {
                "schema": "tester-spin/pragmatic-protocol-observations/v1",
                "responses_analyzed": 5,
                "unknown_signatures": unknown or [],
                "unhandled_signatures": unhandled or [],
            }
        ),
        encoding="utf-8",
    )
    (root / "path-coverage.json").write_text(
        json.dumps(
            {
                "schema": "tester-spin/path-coverage/v1",
                "complete": path_complete,
                "missing_count": 0 if path_complete else 1,
                "branch_points": [] if path_complete else [
                    {"mode_id": "PURCHASE_1", "missing": ["1"], "complete": False}
                ],
            }
        ),
        encoding="utf-8",
    )


class PragmaticActionInventoryTests(unittest.TestCase):
    def test_complete_when_authoritative_root_modes_and_observed_paths_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            result = _result(tmp)

            annotate_pragmatic_action_inventory(result)

        inventory = result.structural_map["action_inventory"]
        self.assertEqual(inventory["state"], "COMPLETE")
        self.assertEqual(inventory["source"], "pragmatic-doInit+runtime-evidence")
        self.assertEqual(inventory["root_actions"], ["PURCHASE_1", "SPIN"])
        self.assertTrue(result.structural_map["existing"]["preserved"])

    def test_missing_current_run_artifacts_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = _result(tmp)
            annotate_pragmatic_action_inventory(result)

        inventory = result.structural_map["action_inventory"]
        self.assertEqual(inventory["state"], "UNKNOWN")
        self.assertIn("modes.json", inventory["reason"])

    def test_authoritative_root_mode_mismatch_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(
                root,
                catalog_modes=[
                    {"id": "SPIN", "kind": "SPIN", "enabled": True},
                    {"id": "PURCHASE_1", "kind": "PURCHASE", "enabled": True},
                    {"id": "PURCHASE_2", "kind": "PURCHASE", "enabled": True},
                ],
            )
            result = _result(tmp)
            annotate_pragmatic_action_inventory(result)

        inventory = result.structural_map["action_inventory"]
        self.assertEqual(inventory["state"], "INCOMPLETE")
        self.assertEqual(inventory["missing_root_actions"], ["PURCHASE_2"])

    def test_unknown_or_unhandled_protocol_signature_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root, unknown=[{"signature": "abc"}])
            result = _result(tmp)
            annotate_pragmatic_action_inventory(result)

        inventory = result.structural_map["action_inventory"]
        self.assertEqual(inventory["state"], "INCOMPLETE")
        self.assertIn("firma", inventory["reason"].lower())

    def test_incomplete_path_coverage_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root, path_complete=False)
            result = _result(tmp)
            annotate_pragmatic_action_inventory(result)

        inventory = result.structural_map["action_inventory"]
        self.assertEqual(inventory["state"], "INCOMPLETE")
        self.assertIn("ramas", inventory["reason"].lower())


if __name__ == "__main__":
    unittest.main()
