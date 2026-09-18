from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.farm_contract import export_farm_contract
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
        spin_mode = {
            "id": "SPIN",
            "kind": "SPIN",
            "wire_command": "spin",
            "observed": True,
            "executable": True,
        }
        if provider_key == "bgaming" and status == "OK":
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
            discovered_modes=[spin_mode],
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

    @staticmethod
    def _prepare_bgaming_profile(game_dir: Path) -> None:
        (game_dir / "game.json").write_text(
            json.dumps(
                {
                    "provider": "bgaming",
                    "slug": "synthetic",
                    "name": "Synthetic",
                    "public_url": "https://bgaming.com/games/synthetic/",
                    "identifier": "Synthetic",
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

    def test_rubyplay_farm_contract_rejects_finite_choice_without_domain_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            provider = RubyPlayProvider(root)
            game = self._game("rubyplay")
            result = self._result("rubyplay")
            result.discovered_modes.append(
                {
                    "id": "SPIN__SELECT_INDEX_DOMAIN",
                    "kind": "INDEXED_CHOICE",
                    "parent": "SPIN",
                    "prefix": [],
                    "wire_command": "select",
                    "observed": True,
                    "executable": True,
                    "coverage_required": True,
                    "required_options": ["0", "1"],
                    "covered_options": ["0", "1"],
                    "required_samples": 1,
                    "sample_counts": {"0": 1, "1": 1},
                }
            )

            contract = provider.build_farm_contract(game, result)

        self.assertFalse(contract["ready"])
        self.assertIn(
            "RUBYPLAY_CHOICE_DOMAIN_UNPROVEN:SPIN__SELECT_INDEX_DOMAIN",
            contract["unresolved"],
        )
        choice = next(
            item
            for item in contract["execution_structure"]["choices"]
            if item["mode_id"] == "SPIN__SELECT_INDEX_DOMAIN"
        )
        self.assertFalse(choice["coverage_complete"])
        self.assertIn(
            "RUBYPLAY_CHOICE_DOMAIN_UNPROVEN:SPIN__SELECT_INDEX_DOMAIN",
            provider.validate_farm_contract(contract),
        )

    def test_rubyplay_farm_contract_accepts_authoritative_rejection_window_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            provider = RubyPlayProvider(root)
            game = self._game("rubyplay")
            result = self._result("rubyplay")
            result.discovered_modes.append(
                {
                    "id": "SPIN__SELECT_INDEX_DOMAIN",
                    "kind": "INDEXED_CHOICE",
                    "parent": "SPIN",
                    "prefix": [],
                    "wire_command": "select",
                    "observed": True,
                    "executable": True,
                    "coverage_required": True,
                    "domain_authority": "isolated-live-server-rejection-window",
                    "boundary_index": 2,
                    "boundary_confirmations": 2,
                    "rejection_span": 2,
                    "required_options": ["0", "1"],
                    "covered_options": ["0", "1"],
                    "required_samples": 1,
                    "sample_counts": {"0": 1, "1": 1},
                    "observed_indices": [0, 1],
                }
            )

            contract = provider.build_farm_contract(game, result)

        self.assertTrue(contract["ready"], contract["unresolved"])
        choice = next(
            item
            for item in contract["execution_structure"]["choices"]
            if item["mode_id"] == "SPIN__SELECT_INDEX_DOMAIN"
        )
        self.assertTrue(choice["coverage_complete"])
        self.assertEqual(provider.validate_farm_contract(contract), [])

    def test_every_active_provider_promotes_ready_contract_through_exporter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for provider in self._providers(root):
                with self.subTest(provider=provider.key):
                    game = self._game(provider.key)
                    game_dir = provider.game_dir(game)
                    if provider.key == "bgaming":
                        self._prepare_bgaming_profile(game_dir)
                    log: list[str] = []
                    export_farm_contract(
                        provider,
                        game,
                        self._result(provider.key),
                        progress=log.append,
                    )
                    self.assertTrue(
                        (game_dir / "analysis" / "farm-contract-candidate.json").is_file(),
                        log,
                    )
                    self.assertTrue((game_dir / "farm-contract.json").is_file(), log)
                    self.assertIn("farm contract: PROMOTED", log)


if __name__ == "__main__":
    unittest.main()
