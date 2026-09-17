from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tester_spin.feature_sessions import (
    attach_feature_session_report,
    finalize_feature_session_report,
    make_feature_round,
    make_feature_session,
)
from tester_spin.models import GameTestResult
from tester_spin.purchase_coverage import (
    PURCHASE_COMPLETE,
    PURCHASE_UNKNOWN,
    finalize_purchase_coverage,
    make_purchase_option,
)


def _result(root: Path) -> GameTestResult:
    return GameTestResult(
        provider="synthetic",
        slug="synthetic",
        game_name="Synthetic",
        game_url="https://example.invalid/game",
        requested_spins=1,
        successful_spins=1,
        failed_spins=0,
        status="OK",
        run_dir=str(root),
    )


def _complete_option() -> dict:
    return make_purchase_option(
        "PURCHASE_A",
        provider_selector={"id": "a"},
        source_kind="synthetic-wire",
        executable=True,
        wire_contract_state="PROVEN",
        execution_state="COMPLETE",
        terminal=True,
        reason="root purchase completed",
    )


class FeatureSessionPurchaseGateTests(unittest.TestCase):
    def test_incomplete_observed_feature_downgrades_complete_root_purchase(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = _result(Path(temp))
            session = make_feature_session(
                session_id="PURCHASE_A:1",
                trigger="PURCHASE",
                parent_mode="PURCHASE_A",
                attempt_number=1,
                entry={"command": "buy"},
                rounds=[make_feature_round(1, provider_action="freespin", source="wire")],
                terminal_proven=False,
                returned_to_base=False,
                wire_steps=2,
            )
            attach_feature_session_report(
                result,
                finalize_feature_session_report(result, sessions=[session], authority="synthetic"),
            )

            coverage = finalize_purchase_coverage(
                result,
                options=[_complete_option()],
                inventory_state="COMPLETE",
                authority="synthetic",
            )

        self.assertEqual(coverage["state"], PURCHASE_UNKNOWN)
        self.assertEqual(coverage["options"][0]["execution_state"], "UNKNOWN")
        self.assertTrue(coverage["options"][0]["terminal"])
        self.assertIn("feature session", coverage["options"][0]["reason"].lower())

    def test_complete_observed_feature_preserves_complete_purchase(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = _result(Path(temp))
            session = make_feature_session(
                session_id="PURCHASE_A:1",
                trigger="PURCHASE",
                parent_mode="PURCHASE_A",
                attempt_number=1,
                entry={"command": "buy"},
                rounds=[make_feature_round(1, provider_action="freespin", source="wire")],
                terminal_proven=True,
                returned_to_base=True,
                wire_steps=2,
            )
            attach_feature_session_report(
                result,
                finalize_feature_session_report(result, sessions=[session], authority="synthetic"),
            )

            coverage = finalize_purchase_coverage(
                result,
                options=[_complete_option()],
                inventory_state="COMPLETE",
                authority="synthetic",
            )

        self.assertEqual(coverage["state"], PURCHASE_COMPLETE)
        self.assertEqual(coverage["options"][0]["execution_state"], "COMPLETE")

    def test_no_observed_feature_does_not_block_one_step_purchase(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = _result(Path(temp))
            coverage = finalize_purchase_coverage(
                result,
                options=[_complete_option()],
                inventory_state="COMPLETE",
                authority="synthetic",
            )
        self.assertEqual(coverage["state"], PURCHASE_COMPLETE)


if __name__ == "__main__":
    unittest.main()
