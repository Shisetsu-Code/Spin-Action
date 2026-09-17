from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import GameTestResult
from tester_spin.providers.rubyplay.choice_exhaustive import apply_rubyplay_choice_audit


class RubyPlayChoiceAuditProofTests(unittest.TestCase):
    def test_proven_parent_domain_is_not_replaced_by_unresolved_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = root / "PURCHASE_SELECT" / "attempt-00001"
            attempt.mkdir(parents=True)
            (attempt / "step-002-request.json").write_text(
                json.dumps({"action": "select", "index": 0}),
                encoding="utf-8",
            )
            result = GameTestResult(
                provider="rubyplay",
                slug="synthetic",
                game_name="Synthetic",
                game_url="https://example.invalid/game",
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="OK",
                run_dir=str(root),
                discovered_modes=[
                    {
                        "id": "PURCHASE_SELECT__SELECT_INDEX_DOMAIN",
                        "kind": "INDEXED_CHOICE",
                        "parent": "PURCHASE_SELECT",
                        "prefix": [],
                        "wire_command": "select",
                        "coverage_required": True,
                        "required_options": ["0", "1"],
                        "covered_options": ["0", "1"],
                        "domain_authority": "isolated-live-server-rejection-window",
                        "boundary_index": 2,
                        "boundary_confirmations": 2,
                        "rejection_span": 2,
                    }
                ],
            )

            apply_rubyplay_choice_audit(result, progress=lambda _message: None)

        indexed = [
            row for row in result.discovered_modes
            if row.get("kind") == "INDEXED_CHOICE"
        ]
        self.assertEqual(len(indexed), 1)
        self.assertEqual(indexed[0]["required_options"], ["0", "1"])
        self.assertEqual(indexed[0]["covered_options"], ["0", "1"])
        self.assertEqual(result.status, "OK")
        self.assertNotIn("DOMAIN_UNRESOLVED", str(result.discovered_modes))

    def test_missing_proof_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = root / "PURCHASE_SELECT" / "attempt-00001"
            attempt.mkdir(parents=True)
            (attempt / "step-002-request.json").write_text(
                json.dumps({"action": "select", "index": 0}),
                encoding="utf-8",
            )
            result = GameTestResult(
                provider="rubyplay",
                slug="synthetic",
                game_name="Synthetic",
                game_url="https://example.invalid/game",
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="OK",
                run_dir=str(root),
            )

            apply_rubyplay_choice_audit(result, progress=lambda _message: None)

        self.assertEqual(result.status, "PARCIAL")
        self.assertIn("DOMAIN_UNRESOLVED", str(result.discovered_modes))

    def test_second_select_prefix_without_proof_keeps_result_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = root / "PURCHASE_SELECT" / "attempt-00001"
            attempt.mkdir(parents=True)
            (attempt / "step-002-request.json").write_text(
                json.dumps({"action": "select", "index": 1}),
                encoding="utf-8",
            )
            (attempt / "step-003-request.json").write_text(
                json.dumps({"action": "freespin"}),
                encoding="utf-8",
            )
            (attempt / "step-004-request.json").write_text(
                json.dumps({"action": "select", "index": 0}),
                encoding="utf-8",
            )
            result = GameTestResult(
                provider="rubyplay",
                slug="synthetic",
                game_name="Synthetic",
                game_url="https://example.invalid/game",
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="OK",
                run_dir=str(root),
                discovered_modes=[
                    {
                        "id": "PURCHASE_SELECT__SELECT_INDEX_DOMAIN",
                        "kind": "INDEXED_CHOICE",
                        "parent": "PURCHASE_SELECT",
                        "prefix": [],
                        "wire_command": "select",
                        "coverage_required": True,
                        "required_options": ["0", "1"],
                        "covered_options": ["0", "1"],
                    }
                ],
            )

            apply_rubyplay_choice_audit(result, progress=lambda _message: None)

        self.assertEqual(result.status, "PARCIAL")
        indexed = [row for row in result.discovered_modes if row.get("kind") == "INDEXED_CHOICE"]
        by_prefix = {tuple(row.get("prefix") or []): row for row in indexed}
        self.assertEqual(by_prefix[()]["required_options"], ["0", "1"])
        self.assertEqual(
            by_prefix[("select=1",)]["required_options"],
            ["DOMAIN_UNRESOLVED"],
        )


if __name__ == "__main__":
    unittest.main()
