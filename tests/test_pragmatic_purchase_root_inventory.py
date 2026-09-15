from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.purchase_coverage import PURCHASE_COMPLETE
from tester_spin.providers.pragmatic_purchase_coverage import build_pragmatic_purchase_coverage


class PragmaticPurchaseRootInventoryTests(unittest.TestCase):
    def test_terminal_exact_purchase_closes_even_when_general_branch_inventory_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt_dir = root / "PURCHASE_1" / "attempt-00001"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "entry.request.txt").write_text(
                "action=doSpin&symbol=vswayscharms&pur=0&bl=0",
                encoding="utf-8",
            )
            result = GameTestResult(
                provider="pragmatic",
                slug="frozen-charms",
                game_name="Frozen Charms",
                game_url="https://www.pragmaticplay.com/en/games/frozen-charms/",
                requested_spins=8,
                successful_spins=3,
                failed_spins=5,
                status="PARCIAL",
                symbol="vswayscharms",
                run_dir=str(root),
                structural_map={
                    "action_inventory": {
                        "state": "INCOMPLETE",
                        "reason": "Cobertura FSO incompleta",
                    }
                },
                discovered_modes=[
                    {
                        "id": "SPIN",
                        "kind": "SPIN",
                        "enabled": True,
                    },
                    {
                        "id": "ANTE_BET_1",
                        "kind": "ANTE_BET",
                        "enabled": True,
                    },
                    {
                        "id": "PURCHASE_1",
                        "kind": "PURCHASE",
                        "enabled": True,
                        "provider_pur": 0,
                        "price_known": True,
                        "paid_cost": 200.0,
                        "price_x_base": 100.0,
                        "source_field": "purInit[0]",
                        "source_value": "2000",
                    },
                    {
                        "id": "PURCHASE_1__FSO_BRANCH_ROOT",
                        "kind": "CONTINUATION",
                        "coverage_required": True,
                        "required_options": ["0", "1", "2", "3", "4", "5"],
                        "covered_options": ["0"],
                    },
                ],
                attempts=[
                    SpinAttempt(
                        number=1,
                        ok=True,
                        mode_id="PURCHASE_1",
                        mode_kind="PURCHASE",
                        status_code=200,
                        terminal=True,
                        wire_steps=37,
                        artifact_dir=str(attempt_dir),
                    )
                ],
            )
            coverage = build_pragmatic_purchase_coverage(result)
            self.assertEqual(coverage["state"], PURCHASE_COMPLETE)
            self.assertEqual(coverage["counts"]["complete"], 1)
            self.assertEqual(coverage["counts"]["unknown"], 0)


if __name__ == "__main__":
    unittest.main()
