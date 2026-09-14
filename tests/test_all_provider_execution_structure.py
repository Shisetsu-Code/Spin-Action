from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import Game, GameTestResult, SpinAttempt
from tester_spin.providers import (
    BGamingProvider,
    BelatraProvider,
    OneSpin4WinProvider,
    PragmaticProvider,
    RedTigerProvider,
    RubyPlayProvider,
)
from tester_spin.providers.bgaming.profile import API_V2, PROFILE_SCHEMA


class AllProviderExecutionStructureTests(unittest.TestCase):
    def _providers(self, root: Path):
        return [
            PragmaticProvider(root),
            OneSpin4WinProvider(root),
            BelatraProvider(root),
            BGamingProvider(root),
            RubyPlayProvider(root),
            RedTigerProvider(root),
        ]

    @staticmethod
    def _game(provider_key: str) -> Game:
        urls = {
            "pragmatic": "https://www.pragmaticplay.com/en/games/synthetic/",
            "1spin4win": "https://www.1spin4win.com/games/synthetic",
            "belatra": "https://belatragames.com/es/games/game/synthetic",
            "bgaming": "https://bgaming.com/games/synthetic/",
            "rubyplay": "https://rubyplay.com/games/synthetic/",
            "redtiger": "https://games.evolution.com/slots/synthetic/",
        }
        symbols = {
            "pragmatic": "vs20synthetic",
            "1spin4win": "synthetic",
            "belatra": "synthetic",
            "bgaming": "Synthetic",
            "rubyplay": "Synthetic",
            "redtiger": "123456",
        }
        return Game(
            provider=provider_key,
            slug="synthetic",
            name="Synthetic",
            url=urls[provider_key],
            symbol=symbols[provider_key],
        )

    @staticmethod
    def _result(provider_key: str) -> GameTestResult:
        spin_mode = {
            "id": "SPIN",
            "kind": "SPIN",
            "wire_command": "spin",
            "observed": True,
            "executable": True,
        }
        if provider_key == "bgaming":
            spin_mode.update(
                evidence_level="REMOTE_EXECUTION",
                execution_state="PROVEN_TERMINAL",
                validated=True,
            )
        return GameTestResult(
            provider=provider_key,
            slug="synthetic",
            game_name="Synthetic",
            game_url="https://example.invalid/synthetic",
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
            symbol={
                "pragmatic": "vs20synthetic",
                "1spin4win": "synthetic",
                "belatra": "synthetic",
                "bgaming": "Synthetic",
                "rubyplay": "Synthetic",
                "redtiger": "123456",
            }[provider_key],
            discovered_modes=[spin_mode],
            attempts=[
                SpinAttempt(
                    number=1,
                    ok=True,
                    mode_id="SPIN",
                    mode_kind="SPIN",
                    terminal=True,
                )
            ],
            finished_at="2026-09-14T08:10:00+00:00",
        )

    @staticmethod
    def _prepare_bgaming(game_dir: Path, game: Game) -> None:
        (game_dir / "game.json").write_text(
            json.dumps(
                {
                    "provider": "bgaming",
                    "slug": game.slug,
                    "name": game.name,
                    "public_url": game.url,
                    "identifier": game.symbol,
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

    def test_every_active_provider_exports_machine_readable_wager_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            providers = self._providers(root)
            try:
                self.assertEqual(
                    {provider.key for provider in providers},
                    {"pragmatic", "1spin4win", "belatra", "bgaming", "rubyplay", "redtiger"},
                )
                for provider in providers:
                    with self.subTest(provider=provider.key):
                        game = self._game(provider.key)
                        if provider.key == "bgaming":
                            self._prepare_bgaming(provider.game_dir(game), game)
                        contract = provider.build_farm_contract(game, self._result(provider.key))
                        structure = contract.get("execution_structure")
                        self.assertIsInstance(structure, dict)
                        wagers = structure.get("wagers")
                        choices = structure.get("choices")
                        domains = structure.get("provider_domains")
                        self.assertIsInstance(wagers, list)
                        self.assertIsInstance(choices, list)
                        self.assertIsInstance(domains, dict)
                        self.assertTrue(
                            any(item.get("mode_id") == "SPIN" for item in wagers),
                            structure,
                        )
            finally:
                for provider in providers:
                    http = getattr(provider, "http", None)
                    close = getattr(http, "close", None)
                    if callable(close):
                        close()


if __name__ == "__main__":
    unittest.main()
