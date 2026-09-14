from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import Game, GameTestResult, SpinAttempt
from tester_spin.providers.bgaming.farm_contract import build_bgaming_farm_contract
from tester_spin.providers.bgaming.profile import API_V2, PROFILE_SCHEMA
from tester_spin.providers.result_farm_contract import (
    ProviderFarmSpec,
    build_result_farm_contract,
)


class FarmContractStructureTests(unittest.TestCase):
    def test_generic_contract_exposes_wagers_and_choice_domains(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = Game(
                provider="synthetic",
                slug="game-a",
                name="Game A",
                url="https://example.invalid/game-a",
                symbol="GAME_A",
            )
            result = GameTestResult(
                provider="synthetic",
                slug="game-a",
                game_name="Game A",
                game_url=game.url,
                requested_spins=2,
                successful_spins=2,
                failed_spins=0,
                status="OK",
                symbol="GAME_A",
                finished_at="2026-09-14T08:00:00+00:00",
                discovered_modes=[
                    {
                        "id": "SPIN",
                        "kind": "SPIN",
                        "wire_command": "spin",
                        "observed": True,
                        "executable": True,
                        "allowed_bets": [1, 2, 5],
                        "default_bet": 2,
                    },
                    {
                        "id": "PURCHASE_BONUS",
                        "kind": "PURCHASE",
                        "wire_command": "buy",
                        "observed": True,
                        "executable": True,
                        "cost": 100,
                        "selector": "bonus",
                    },
                    {
                        "id": "PURCHASE_BONUS__CHOICE_ROOT",
                        "kind": "FSO_BRANCH",
                        "parent": "PURCHASE_BONUS",
                        "prefix": [],
                        "wire_command": "choose",
                        "observed": True,
                        "executable": True,
                        "coverage_required": True,
                        "required_options": ["0", "1"],
                        "covered_options": ["0", "1"],
                        "required_samples": 1,
                        "sample_counts": {"0": 1, "1": 1},
                    },
                ],
                attempts=[
                    SpinAttempt(
                        number=1,
                        ok=True,
                        mode_id="SPIN",
                        mode_kind="SPIN",
                        terminal=True,
                    ),
                    SpinAttempt(
                        number=2,
                        ok=True,
                        mode_id="PURCHASE_BONUS",
                        mode_kind="PURCHASE",
                        terminal=True,
                    ),
                ],
            )
            spec = ProviderFarmSpec(
                provider="synthetic",
                protocol_family="synthetic-v1",
                bootstrap_strategy="synthetic-public",
                transport="http-json",
                terminal_contract={"type": "provider", "name": "synthetic"},
                mode_option_keys=("allowed_bets", "default_bet", "cost", "selector"),
            )

            contract = build_result_farm_contract(game, result, root, spec)
            structure = contract["execution_structure"]

            wagers = {item["mode_id"]: item for item in structure["wagers"]}
            self.assertEqual(wagers["SPIN"]["parameters"]["allowed_bets"], [1, 2, 5])
            self.assertEqual(wagers["SPIN"]["parameters"]["default_bet"], 2)
            self.assertEqual(wagers["PURCHASE_BONUS"]["parameters"]["cost"], 100)
            self.assertEqual(wagers["PURCHASE_BONUS"]["parameters"]["selector"], "bonus")

            self.assertEqual(len(structure["choices"]), 1)
            choice = structure["choices"][0]
            self.assertEqual(choice["mode_id"], "PURCHASE_BONUS__CHOICE_ROOT")
            self.assertEqual(choice["parent"], "PURCHASE_BONUS")
            self.assertEqual(choice["domain"], ["0", "1"])
            self.assertEqual(choice["covered"], ["0", "1"])
            self.assertTrue(choice["coverage_complete"])

    def test_bgaming_contract_exposes_profile_bet_and_choice_domains(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp)
            game = Game(
                provider="bgaming",
                slug="game-a",
                name="Game A",
                url="https://bgaming.com/games/game-a/",
                symbol="GameA",
            )
            (game_dir / "game.json").write_text(
                json.dumps(
                    {
                        "provider": "bgaming",
                        "slug": "game-a",
                        "name": "Game A",
                        "public_url": game.url,
                        "identifier": "GameA",
                        "provider_protocol": {
                            "schema": PROFILE_SCHEMA,
                            "capability_version": 2,
                            "family": API_V2,
                            "confidence": 1.0,
                            "evidence": ["init.api_version=2"],
                            "spin_options": {"mode": "base"},
                            "command_options": {"spin": {"rows": 5}},
                            "request_extra_data": {},
                            "spin_option_choices": {"level": [0, 1, 2]},
                            "effective_bet_selector": "level",
                            "effective_bet_multipliers": {"0": 1, "1": 2, "2": 5},
                            "dynamic_purchased_feature": True,
                            "purchase_feature_level_supported": True,
                            "purchase_features": ["bonus_buy"],
                            "rows_required": True,
                            "line_count": 0,
                            "variable_layout": False,
                            "allowed_continuations": ["freespin"],
                            "source": "init",
                            "bundle_sha256": "abc123",
                            "discovery_diagnostics": [],
                            "validated": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = GameTestResult(
                provider="bgaming",
                slug="game-a",
                game_name="Game A",
                game_url=game.url,
                requested_spins=2,
                successful_spins=2,
                failed_spins=0,
                status="OK",
                symbol="GameA",
                finished_at="2026-09-14T08:00:00+00:00",
                discovered_modes=[
                    {
                        "id": "SPIN",
                        "kind": "SPIN",
                        "wire_command": "spin",
                        "observed": True,
                        "executable": True,
                        "evidence_level": "REMOTE_EXECUTION",
                        "execution_state": "PROVEN_TERMINAL",
                        "validated": True,
                    },
                    {
                        "id": "PURCHASE_BONUS_BUY",
                        "kind": "PURCHASE",
                        "wire_command": "spin",
                        "purchased_feature": "bonus_buy",
                        "cost_multiplier": 100,
                        "observed": True,
                        "executable": True,
                        "evidence_level": "REMOTE_EXECUTION",
                        "execution_state": "PROVEN_TERMINAL",
                        "validated": True,
                    },
                ],
            )

            contract = build_bgaming_farm_contract(game, result, game_dir)
            structure = contract["execution_structure"]
            domains = structure["provider_domains"]

            self.assertEqual(domains["spin_option_choices"], {"level": [0, 1, 2]})
            self.assertEqual(domains["effective_bet_selector"], "level")
            self.assertEqual(domains["effective_bet_multipliers"], {"0": 1, "1": 2, "2": 5})
            self.assertEqual(domains["purchase_features"], ["bonus_buy"])

            wagers = {item["mode_id"]: item for item in structure["wagers"]}
            self.assertEqual(wagers["SPIN"]["parameters"]["mode"], "base")
            self.assertEqual(wagers["SPIN"]["parameters"]["rows"], 5)
            self.assertEqual(wagers["PURCHASE_BONUS_BUY"]["cost_multiplier"], 100)
            self.assertEqual(
                wagers["PURCHASE_BONUS_BUY"]["parameters"]["purchased_feature"],
                "bonus_buy",
            )


if __name__ == "__main__":
    unittest.main()
