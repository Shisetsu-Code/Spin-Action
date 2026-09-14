from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers import BGamingProvider
from tester_spin.providers.bgaming.profile import API_V2, PROFILE_SCHEMA
from tester_spin.providers.farm_structure import build_execution_structure


class FarmContractStructureTests(unittest.TestCase):
    def test_generic_structure_exposes_wagers_and_choice_domains(self) -> None:
        structure = build_execution_structure(
            [
                {
                    "id": "SPIN",
                    "kind": "SPIN",
                    "evidence": "DEMOSTRADO",
                    "executor": "spin",
                    "options": {"allowed_bets": [1, 2, 5], "default_bet": 2},
                },
                {
                    "id": "PURCHASE_BONUS",
                    "kind": "PURCHASE",
                    "evidence": "DEMOSTRADO",
                    "executor": "buy",
                    "options": {"cost": 100, "selector": "bonus"},
                },
                {
                    "id": "PURCHASE_BONUS__CHOICE_ROOT",
                    "kind": "FSO_BRANCH",
                    "evidence": "DEMOSTRADO",
                    "executor": "choose",
                    "options": {
                        "parent": "PURCHASE_BONUS",
                        "prefix": [],
                        "required_options": ["0", "1"],
                        "covered_options": ["0", "1"],
                        "required_samples": 1,
                        "sample_counts": {"0": 1, "1": 1},
                    },
                },
            ]
        )

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

    def test_bgaming_provider_exposes_profile_bet_and_choice_domains(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = BGamingProvider(Path(temp))
            try:
                game = Game(
                    provider="bgaming",
                    slug="game-a",
                    name="Game A",
                    url="https://bgaming.com/games/game-a/",
                    symbol="GameA",
                )
                game_dir = provider.game_dir(game)
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

                contract = provider.build_farm_contract(game, result)
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
            finally:
                provider.http.close()


if __name__ == "__main__":
    unittest.main()
