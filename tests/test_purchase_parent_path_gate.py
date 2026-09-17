from __future__ import annotations

import unittest

from tester_spin.models import GameTestResult
from tester_spin.purchase_coverage import (
    PURCHASE_COMPLETE,
    PURCHASE_UNKNOWN,
    finalize_purchase_coverage,
    make_purchase_option,
)


def _result() -> GameTestResult:
    return GameTestResult(
        provider="synthetic",
        slug="g",
        game_name="G",
        game_url="https://example.invalid/g",
        requested_spins=1,
        successful_spins=1,
        failed_spins=0,
        status="PARCIAL",
    )


def _purchase(purchase_id: str) -> dict:
    return make_purchase_option(
        purchase_id,
        executable=True,
        wire_contract_state="PROVEN",
        execution_state="COMPLETE",
        terminal=True,
        reason="root closed",
    )


class PurchaseParentPathGateTests(unittest.TestCase):
    def test_uncovered_child_branch_blocks_parent_purchase_complete(self) -> None:
        result = _result()
        result.discovered_modes = [
            {
                "id": "PURCHASE_A__SELECT_ROOT",
                "kind": "INDEXED_CHOICE",
                "parent": "PURCHASE_A",
                "coverage_required": True,
                "required_options": ["0", "1"],
                "covered_options": ["0"],
            }
        ]

        coverage = finalize_purchase_coverage(
            result,
            options=[_purchase("PURCHASE_A")],
            inventory_state="COMPLETE",
            authority="synthetic",
        )

        self.assertEqual(coverage["state"], PURCHASE_UNKNOWN)
        self.assertEqual(coverage["options"][0]["execution_state"], "UNKNOWN")
        self.assertIn("child", coverage["options"][0]["reason"].lower())

    def test_unresolved_child_domain_blocks_parent_purchase_complete(self) -> None:
        result = _result()
        result.discovered_modes = [
            {
                "id": "PURCHASE_A__PICK_ROOT",
                "kind": "INDEXED_CHOICE",
                "parent": "PURCHASE_A",
                "coverage_required": True,
                "required_options": ["DOMAIN_UNRESOLVED"],
                "covered_options": [],
            }
        ]

        coverage = finalize_purchase_coverage(
            result,
            options=[_purchase("PURCHASE_A")],
            inventory_state="COMPLETE",
            authority="synthetic",
        )

        self.assertEqual(coverage["state"], PURCHASE_UNKNOWN)

    def test_open_branch_under_other_parent_does_not_block_this_purchase(self) -> None:
        result = _result()
        result.discovered_modes = [
            {
                "id": "PURCHASE_B__SELECT_ROOT",
                "kind": "INDEXED_CHOICE",
                "parent": "PURCHASE_B",
                "coverage_required": True,
                "required_options": ["0", "1"],
                "covered_options": ["0"],
            }
        ]

        coverage = finalize_purchase_coverage(
            result,
            options=[_purchase("PURCHASE_A")],
            inventory_state="COMPLETE",
            authority="synthetic",
        )

        self.assertEqual(coverage["state"], PURCHASE_COMPLETE)
        self.assertEqual(coverage["options"][0]["execution_state"], "COMPLETE")

    def test_malformed_child_sample_count_fails_closed_without_exception(self) -> None:
        result = _result()
        result.discovered_modes = [
            {
                "id": "PURCHASE_A__SELECT_ROOT",
                "kind": "INDEXED_CHOICE",
                "parent": "PURCHASE_A",
                "coverage_required": True,
                "required_options": ["0"],
                "covered_options": ["0"],
                "required_samples": 2,
                "sample_counts": {"0": "not-a-number"},
            }
        ]

        coverage = finalize_purchase_coverage(
            result,
            options=[_purchase("PURCHASE_A")],
            inventory_state="COMPLETE",
            authority="synthetic",
        )

        self.assertEqual(coverage["state"], PURCHASE_UNKNOWN)

    def test_sample_deficit_on_child_branch_blocks_parent_purchase(self) -> None:
        result = _result()
        result.discovered_modes = [
            {
                "id": "PURCHASE_A__SELECT_ROOT",
                "kind": "INDEXED_CHOICE",
                "parent": "PURCHASE_A",
                "coverage_required": True,
                "required_options": ["0", "1"],
                "covered_options": ["0", "1"],
                "required_samples": 2,
                "sample_counts": {"0": 2, "1": 1},
            }
        ]

        coverage = finalize_purchase_coverage(
            result,
            options=[_purchase("PURCHASE_A")],
            inventory_state="COMPLETE",
            authority="synthetic",
        )

        self.assertEqual(coverage["state"], PURCHASE_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
