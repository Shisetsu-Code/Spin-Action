from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers.bgaming.contracts import ChoiceContract, CommandContract, COMMAND_CONTRACTS
from tester_spin.providers.bgaming_path_policy import wager_plan_from_init
from tester_spin.sample_catalog import build_sample_catalog


class SamplingCatalogV2Tests(unittest.TestCase):
    def test_bgaming_choice_uses_contract_option_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = root / "MODE" / "attempt-001"
            attempt.mkdir(parents=True)
            (attempt / "step-001-request.json").write_text(
                json.dumps({"command": "pick_cards", "options": {"index": 2}}),
                encoding="utf-8",
            )
            (attempt / "step-001-response.json").write_text(
                json.dumps({"flow": {"state": "closed"}}),
                encoding="utf-8",
            )
            result = GameTestResult(
                provider="bgaming",
                slug="synthetic",
                game_name="Synthetic",
                game_url="https://example.invalid",
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="OK",
                run_dir=str(root),
                attempts=[SpinAttempt(number=1, ok=True, terminal=True, mode_id="MODE", artifact_dir=str(attempt))],
            )
            synthetic = CommandContract(
                "pick_cards",
                choice=ChoiceContract(option_field="index", container_path=("game", "choices"), value_field="index"),
            )
            with patch.dict(COMMAND_CONTRACTS, {"pick_cards": synthetic}):
                catalog = build_sample_catalog(result)
            self.assertEqual(
                catalog["groups"][0]["choices"],
                [{"command": "pick_cards", "field": "index", "value": 2}],
            )

    def test_pragmatic_choice_sidecar_is_hashed_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = root / "PURCHASE_1" / "attempt-001"
            attempt.mkdir(parents=True)
            sidecar = attempt / "fso-selection-003.json"
            sidecar.write_text(json.dumps({"option_indices": [0, 1], "selected_index": 1}), encoding="utf-8")
            (attempt / "response.json").write_text(json.dumps({"na": "s"}), encoding="utf-8")
            result = GameTestResult(
                provider="pragmatic",
                slug="synthetic",
                game_name="Synthetic",
                game_url="https://example.invalid",
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="OK",
                run_dir=str(root),
                attempts=[SpinAttempt(number=1, ok=True, terminal=True, mode_id="PURCHASE_1", artifact_dir=str(attempt))],
            )
            catalog = build_sample_catalog(result)
            paths = {item["path"] for item in catalog["groups"][0]["samples"][0]["evidence"]}
            self.assertIn("PURCHASE_1/attempt-001/fso-selection-003.json", paths)

    def test_wager_plan_prefers_minimum_provider_advertised_bet(self) -> None:
        plan = wager_plan_from_init({
            "options": {"default_bet": 200, "available_bets": [200, 20, 100]},
            "balance": 10000,
        })
        self.assertEqual(plan.default_bet, 200.0)
        self.assertEqual(plan.coverage_bet, 20.0)
        self.assertEqual(plan.available_bets, [20.0, 100.0, 200.0])


if __name__ == "__main__":
    unittest.main()
