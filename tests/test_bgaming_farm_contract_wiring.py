from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers import BGamingProvider
from tester_spin.providers.bgaming.profile import API_V2, PROFILE_SCHEMA


class BGamingFarmContractWiringTests(unittest.TestCase):
    def test_active_provider_builds_contract_in_its_game_folder(self) -> None:
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
                            "public_url": "https://bgaming.com/games/game-a/",
                            "identifier": "GameA",
                            "provider_protocol": {
                                "schema": PROFILE_SCHEMA,
                                "capability_version": 2,
                                "family": API_V2,
                                "confidence": 1.0,
                                "evidence": ["init.api_version=2"],
                                "spin_options": {},
                                "command_options": {},
                                "request_extra_data": {},
                                "spin_option_choices": {},
                                "effective_bet_selector": "",
                                "effective_bet_multipliers": {},
                                "dynamic_purchased_feature": False,
                                "purchase_feature_level_supported": False,
                                "purchase_features": [],
                                "rows_required": False,
                                "line_count": 0,
                                "variable_layout": False,
                                "allowed_continuations": [],
                                "source": "init",
                                "bundle_sha256": "",
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
                    requested_spins=1,
                    successful_spins=1,
                    failed_spins=0,
                    status="OK",
                    symbol="GameA",
                    discovered_modes=[
                        {
                            "id": "SPIN",
                            "kind": "SPIN",
                            "wire_command": "spin",
                            "observed": True,
                            "evidence_level": "REMOTE_EXECUTION",
                            "execution_state": "PROVEN_TERMINAL",
                            "validated": True,
                        }
                    ],
                )

                self.assertEqual(provider.farm_contract_dir(game), game_dir)
                contract = provider.build_farm_contract(game, result)
                self.assertTrue(contract["ready"], contract["unresolved"])
                self.assertEqual(provider.validate_farm_contract(contract), [])
            finally:
                provider.http.close()


if __name__ == "__main__":
    unittest.main()
