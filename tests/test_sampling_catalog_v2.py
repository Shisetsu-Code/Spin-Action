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

    def test_rare_state_sequence_is_recorded_as_unclassified_wire_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempts = []
            for number in range(1, 5):
                directory = root / "SPIN" / f"attempt-{number:03d}"
                directory.mkdir(parents=True)
                payload = (
                    {"state": "closed", "st": 0}
                    if number < 4
                    else {"state": "feature_entry", "st": 3}
                )
                (directory / "response.json").write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
                attempts.append(
                    SpinAttempt(
                        number=number,
                        ok=True,
                        terminal=True,
                        mode_id="SPIN",
                        mode_kind="SPIN",
                        artifact_dir=str(directory),
                    )
                )

            result = GameTestResult(
                provider="synthetic",
                slug="natural-event",
                game_name="Natural Event",
                game_url="https://example.invalid",
                requested_spins=4,
                successful_spins=4,
                failed_spins=0,
                status="OK",
                run_dir=str(root),
                attempts=attempts,
            )
            catalog = build_sample_catalog(result)
            group = catalog["groups"][0]

            self.assertTrue(catalog["observed_paths_sampled"])
            self.assertEqual(len(group["observed_state_sequences"]), 2)
            dominant = next(
                row for row in group["observed_state_sequences"]
                if row["id"] == group["dominant_state_sequence_id"]
            )
            self.assertEqual(dominant["validated_occurrences"], 3)

            self.assertEqual(len(group["unclassified_wire_variants"]), 1)
            variant = group["unclassified_wire_variants"][0]
            self.assertEqual(variant["classification"], "UNCLASSIFIED_WIRE_VARIANT")
            self.assertTrue(variant["id"].startswith("UNCLASSIFIED_WIRE_VARIANT_"))
            self.assertEqual(variant["validated_occurrences"], 1)
            self.assertEqual(variant["first_attempt"], 4)
            self.assertEqual(variant["evidence"][0]["attempt"], 4)
            self.assertTrue(variant["evidence"][0]["files"][0]["sha256"])
            self.assertNotIn("semantic", variant)

    def test_rubyplay_next_action_chain_is_part_of_wire_sequence_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempts = []
            for number in range(1, 4):
                directory = root / "SPIN" / f"attempt-{number:03d}"
                directory.mkdir(parents=True)
                if number < 3:
                    (directory / "response.json").write_text(
                        json.dumps({"status": "ok", "data": {"next_action": "spin"}}),
                        encoding="utf-8",
                    )
                else:
                    (directory / "response.json").write_text(
                        json.dumps({"status": "ok", "data": {"next_action": "freespin"}}),
                        encoding="utf-8",
                    )
                    (directory / "step-002-response.json").write_text(
                        json.dumps({"status": "ok", "data": {"next_action": "spin"}}),
                        encoding="utf-8",
                    )
                attempts.append(
                    SpinAttempt(
                        number=number,
                        ok=True,
                        terminal=True,
                        mode_id="SPIN",
                        mode_kind="SPIN",
                        artifact_dir=str(directory),
                    )
                )

            result = GameTestResult(
                provider="rubyplay",
                slug="natural-rubyplay-event",
                game_name="Natural RubyPlay Event",
                game_url="https://example.invalid",
                requested_spins=3,
                successful_spins=3,
                failed_spins=0,
                status="OK",
                run_dir=str(root),
                attempts=attempts,
            )

            catalog = build_sample_catalog(result)
            group = catalog["groups"][0]

            self.assertEqual(len(group["observed_state_sequences"]), 2)
            self.assertEqual(len(group["unclassified_wire_variants"]), 1)
            variant = group["unclassified_wire_variants"][0]
            self.assertEqual(variant["first_attempt"], 3)
            sequence = next(
                row for row in group["observed_state_sequences"]
                if row["id"] == variant["sequence_id"]
            )
            observed = [
                tag["value"]
                for state in sequence["observed_state_sequence"]
                for tag in state
                if tag["field"].endswith(".next_action")
            ]
            self.assertEqual(observed, ["freespin", "spin"])

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
