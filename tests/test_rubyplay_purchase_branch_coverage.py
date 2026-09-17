from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.purchase_coverage import PURCHASE_UNKNOWN
from tester_spin.providers.rubyplay.purchase_coverage import build_rubyplay_purchase_coverage


class RubyPlayPurchaseBranchCoverageTests(unittest.TestCase):
    def test_terminal_purchase_with_unresolved_select_domain_is_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt_dir = root / "PURCHASE_SELECT" / "attempt-00001"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "request.json").write_text(
                json.dumps(
                    {
                        "action": "buy_feature",
                        "buy_feature_type": "select",
                        "buy_feature_price": 1000.0,
                    }
                ),
                encoding="utf-8",
            )
            (attempt_dir / "step-00002-request.json").write_text(
                json.dumps(
                    {
                        "action": "select",
                        "index": 0,
                        "buy_feature_type": "select",
                    }
                ),
                encoding="utf-8",
            )

            attempt = SpinAttempt(
                number=1,
                ok=True,
                mode_id="PURCHASE_SELECT",
                mode_kind="PURCHASE",
                status_code=200,
                terminal=True,
                warning="",
                error="",
                artifact_dir=str(attempt_dir),
            )
            result = GameTestResult(
                provider="rubyplay",
                slug="synthetic-select",
                game_name="Synthetic Select",
                game_url="https://example.invalid/synthetic-select",
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="PARCIAL",
                symbol="rp_test",
                discovered_modes=[
                    {
                        "id": "PURCHASE_SELECT",
                        "kind": "PURCHASE",
                        "observed": True,
                        "executable": True,
                        "wire_command": "buy_feature",
                        "buy_feature_type": "select",
                        "feature_multiplier": 100.0,
                        "default_price": 1000.0,
                    },
                    {
                        "id": "RUBYPLAY_SELECT_INDEX_DOMAIN",
                        "kind": "INDEXED_CHOICE",
                        "observed": True,
                        "executable": True,
                        "wire_command": "select",
                        "coverage_required": True,
                        "required_options": ["DOMAIN_UNRESOLVED"],
                        "covered_options": [],
                    },
                ],
                run_dir=str(root),
                attempts=[attempt],
                structural_map={
                    "action_inventory": {
                        "state": "COMPLETE",
                        "source": "rubyplay-active-client+init-session",
                    }
                },
            )

            coverage = build_rubyplay_purchase_coverage(result)

        self.assertEqual(coverage["state"], PURCHASE_UNKNOWN)
        self.assertEqual(coverage["options"][0]["execution_state"], "UNKNOWN")
        self.assertTrue(coverage["options"][0]["terminal"])
        self.assertIn("domain", coverage["options"][0]["reason"].lower())


if __name__ == "__main__":
    unittest.main()
