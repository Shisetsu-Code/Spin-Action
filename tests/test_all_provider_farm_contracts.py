from __future__ import annotations

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


class AllProviderFarmContractTests(unittest.TestCase):
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
    def _result(provider_key: str, *, status: str = "OK") -> GameTestResult:
        return GameTestResult(
            provider=provider_key,
            slug="synthetic",
            game_name="Synthetic",
            game_url="https://example.invalid/synthetic",
            requested_spins=1,
            successful_spins=1 if status == "OK" else 0,
            failed_spins=0 if status == "OK" else 1,
            status=status,
            symbol={
                "pragmatic": "vs20synthetic",
                "1spin4win": "synthetic",
                "belatra": "synthetic",
                "bgaming": "Synthetic",
                "rubyplay": "Synthetic",
                "redtiger": "123456",
            }[provider_key],
            discovered_modes=[
                {
                    "id": "SPIN",
                    "kind": "SPIN",
                    "wire_command": "spin",
                    "observed": True,
                    "executable": True,
                }
            ],
            attempts=[
                SpinAttempt(
                    number=1,
                    ok=status == "OK",
                    mode_id="SPIN",
                    mode_kind="SPIN",
                    terminal=status == "OK",
                    warning="",
                )
            ],
            finished_at="2026-09-14T07:00:00+00:00",
        )

    def test_every_active_provider_opts_into_farm_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            providers = self._providers(root)
            self.assertEqual(
                {provider.key for provider in providers},
                {"pragmatic", "1spin4win", "belatra", "bgaming", "rubyplay", "redtiger"},
            )
            for provider in providers:
                with self.subTest(provider=provider.key):
                    game = self._game(provider.key)
                    self.assertEqual(provider.farm_contract_dir(game), provider.game_dir(game))

    def test_non_bgaming_simple_spin_contracts_are_executable_and_provider_specific(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for provider in self._providers(root):
                if provider.key == "bgaming":
                    continue
                with self.subTest(provider=provider.key):
                    game = self._game(provider.key)
                    contract = provider.build_farm_contract(
                        game,
                        self._result(provider.key),
                    )
                    self.assertEqual(contract["provider"], provider.key)
                    self.assertNotEqual(
                        contract["source"]["protocol_family"],
                        "unsupported",
                    )
                    self.assertNotIn(
                        "PROVIDER_CONTRACT_UNSUPPORTED",
                        contract["unresolved"],
                    )
                    self.assertTrue(contract["ready"], contract["unresolved"])
                    self.assertEqual(provider.validate_farm_contract(contract), [])

    def test_non_bgaming_partial_result_never_promotes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for provider in self._providers(root):
                if provider.key == "bgaming":
                    continue
                with self.subTest(provider=provider.key):
                    game = self._game(provider.key)
                    contract = provider.build_farm_contract(
                        game,
                        self._result(provider.key, status="PARCIAL"),
                    )
                    self.assertFalse(contract["ready"])
                    self.assertTrue(
                        any(
                            reason.startswith("DISCOVERY_STATUS:")
                            for reason in contract["unresolved"]
                        ),
                        contract["unresolved"],
                    )


if __name__ == "__main__":
    unittest.main()
