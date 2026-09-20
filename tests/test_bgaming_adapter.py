from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from tester_spin.models import Game
from tester_spin.providers.bgaming.adapter import BGamingProvider
from tester_spin.providers.bgaming.profile import API_V2, BGamingProfile
from tester_spin.providers.bgaming.runtime import BGamingRuntime


class Response:
    status_code = 200


class BGamingAdapterContractTests(unittest.TestCase):
    def test_client_extra_data_is_present_before_first_init(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = BGamingProvider(Path(temp))
            game = Game(
                provider="bgaming",
                slug="version-negotiated-slot",
                name="Version Negotiated Slot",
                url="https://demo.bgaming-network.com/games/VersionNegotiated/FUN",
                symbol="VersionNegotiated",
            )
            session = requests.Session()
            runtime = BGamingRuntime(
                session=session,
                launch_url="https://demo.bgaming-network.com/games/VersionNegotiated/FUN",
                api_url="https://demo.bgaming-network.com/api/VersionNegotiated/1/session",
                identifier="VersionNegotiated",
                csrf_header_name="X-CSRF-Token",
                csrf_header_value="secret",
                options={},
                round_series_id=777,
            )
            init = {
                "api_version": "2",
                "options": {
                    "default_bet": 90,
                    "available_bets": [90, 225],
                    "layout": {"reels": 5, "rows": 3},
                },
                "balance": {"wallet": 100000, "game": 0},
                "flow": {
                    "command": "init",
                    "state": "ready",
                    "available_actions": ["init", "spin"],
                },
            }
            spin = {
                "api_version": "2",
                "outcome": {
                    "screen": [["1", "2", "3"]] * 5,
                    "bet": 90,
                    "win": 0,
                },
                "balance": {"wallet": 99910, "game": 0},
                "flow": {
                    "round_id": 1,
                    "last_action_id": "1_1",
                    "command": "spin",
                    "state": "closed",
                    "available_actions": ["init", "spin"],
                },
            }
            init_seen_extra = []

            def fake_post(runtime_arg, command, **kwargs):
                if command == "init":
                    init_seen_extra.append(dict(runtime_arg.request_extra_data))
                    return Response(), {
                        "command": "init",
                        "extra_data": {
                            "round_series_id": runtime_arg.round_series_id,
                            **runtime_arg.request_extra_data,
                        },
                    }, init
                return Response(), {"command": command}, spin

            wire = {
                "spin_options": {},
                "request_extra_data": {"api_version": 2},
                "purchase_features": [],
                "required_option_fields": [],
                "source": "https://cdn.bgaming-network.com/game/bundle.js",
                "bundle_sha256": "version-contract",
                "diagnostics": [],
            }

            with (
                patch.object(provider, "_new_session", return_value=session),
                patch(
                    "tester_spin.providers.bgaming.execution.bootstrap_game",
                    return_value=runtime,
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.discover_api_v2_wire_profile",
                    return_value=wire,
                ),
                patch(
                    "tester_spin.providers.bgaming.profile.discover_api_v2_wire_profile",
                    return_value=wire,
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.post_command",
                    side_effect=fake_post,
                ),
            ):
                result = provider.test_game(
                    game,
                    spins=1,
                    timeout_s=5,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

            self.assertEqual(result.status, "OK")
            self.assertEqual(init_seen_extra[0], {"api_version": 2})

    def test_profile_options_are_present_on_first_spin(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = BGamingProvider(Path(temp))
            game = Game(
                provider="bgaming",
                slug="generic-slot",
                name="Generic Slot",
                url="https://demo.bgaming-network.com/play/GenericSlot/FUN?server=demo",
                symbol="GenericSlot",
            )
            session = requests.Session()
            runtime = BGamingRuntime(
                session=session,
                launch_url="https://demo.bgaming-network.com/games/GenericSlot/FUN",
                api_url="https://demo.bgaming-network.com/api/GenericSlot/1/session",
                identifier="GenericSlot",
                csrf_header_name="X-CSRF-Token",
                csrf_header_value="secret",
                options={},
                round_series_id=1,
            )
            init = {
                "api_version": "2",
                "options": {
                    "default_bet": 100,
                    "available_bets": [100, 200],
                    "layout": {"reels": 5, "rows": 3},
                },
                "balance": {"wallet": 100000, "game": 0},
                "flow": {
                    "command": "init",
                    "state": "ready",
                    "available_actions": ["init", "spin"],
                },
            }
            spin = {
                "api_version": "2",
                "outcome": {
                    "screen": [["1", "2", "3"]] * 5,
                    "bet": 100,
                    "win": 0,
                },
                "balance": {"wallet": 99900, "game": 0},
                "flow": {
                    "round_id": 1,
                    "last_action_id": "1_1",
                    "command": "spin",
                    "state": "closed",
                    "available_actions": ["init", "spin"],
                },
            }
            sent_spin_options = []

            def fake_post(_runtime, command, **kwargs):
                if command == "init":
                    return Response(), {"command": "init"}, init
                options = dict(kwargs.get("options") or {})
                sent_spin_options.append(options)
                return Response(), {"command": command, "options": options}, spin

            profile = BGamingProfile(
                family=API_V2,
                confidence=1.0,
                evidence=["test-contract"],
                spin_options={"mode": "60"},
                source="bundle",
                bundle_sha256="bundle-hash",
            )

            with (
                patch.object(provider, "_new_session", return_value=session),
                patch(
                    "tester_spin.providers.bgaming.execution.bootstrap_game",
                    return_value=runtime,
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.discover_profile",
                    return_value=profile,
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.post_command",
                    side_effect=fake_post,
                ),
            ):
                result = provider.test_game(
                    game,
                    spins=1,
                    timeout_s=5,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

            self.assertEqual(result.status, "OK")
            self.assertEqual(result.successful_spins, 1)
            self.assertTrue(result.attempts[0].ok)
            self.assertEqual(len(sent_spin_options), 1)
            self.assertEqual(
                sent_spin_options[0],
                {"bet": 100, "mode": "60"},
            )

    def test_unreported_purchase_cost_is_learned_from_remote_balance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = BGamingProvider(Path(temp))
            game = Game(
                provider="bgaming",
                slug="generic-purchase-slot",
                name="Generic Purchase Slot",
                url="https://demo.bgaming-network.com/play/GenericPurchase/FUN?server=demo",
            )
            session = requests.Session()
            runtime = BGamingRuntime(
                session=session,
                launch_url="https://demo.bgaming-network.com/games/GenericPurchase/FUN",
                api_url="https://demo.bgaming-network.com/api/GenericPurchase/1/session",
                identifier="GenericPurchase",
                csrf_header_name="X-CSRF-Token",
                csrf_header_value="secret",
                options={},
                round_series_id=1,
            )
            init = {
                "api_version": "2",
                "options": {
                    "default_bet": 100,
                    "layout": {"reels": 5, "rows": 3},
                    "feature_options": {
                        "feature_multipliers": {"bonus_buy": 2000},
                        "disabled_features": [],
                    },
                },
                "balance": {"wallet": 100000, "game": 0},
                "flow": {
                    "command": "init",
                    "state": "ready",
                    "available_actions": ["init", "spin"],
                },
            }
            base_spin = {
                "api_version": "2",
                "outcome": {
                    "screen": [["1", "2", "3"]] * 5,
                    "bet": 100,
                    "win": 0,
                },
                "balance": {"wallet": 99900, "game": 0},
                "flow": {
                    "round_id": 1,
                    "last_action_id": "1_1",
                    "command": "spin",
                    "state": "closed",
                    "available_actions": ["init", "spin"],
                },
            }
            purchase_spin = {
                "api_version": "2",
                "outcome": {
                    "screen": [["1", "2", "3"]] * 5,
                    "bet": 100,
                    "win": 0,
                },
                # Purchase runs in a fresh isolated demo session. A x50
                # debit from 100000 therefore finishes at 95000.
                "balance": {"wallet": 95000, "game": 0},
                "flow": {
                    "round_id": 2,
                    "last_action_id": "2_1",
                    "command": "spin",
                    "state": "closed",
                    "available_actions": ["init", "spin"],
                    "purchased_feature": {"name": "bonus_buy"},
                },
            }
            spin_count = 0

            def fake_post(_runtime, command, **kwargs):
                nonlocal spin_count
                if command == "init":
                    return Response(), {"command": "init"}, init
                spin_count += 1
                payload = {
                    "command": "spin",
                    "options": dict(kwargs.get("options") or {}),
                }
                return (
                    Response(),
                    payload,
                    base_spin if spin_count == 1 else purchase_spin,
                )

            with (
                patch.object(provider, "_new_session", return_value=session),
                patch(
                    "tester_spin.providers.bgaming.execution.bootstrap_game",
                    return_value=runtime,
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.discover_profile",
                    return_value=BGamingProfile(
                        family=API_V2,
                        confidence=1.0,
                        source="init",
                        purchase_features=[],
                    ),
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.post_command",
                    side_effect=fake_post,
                ),
            ):
                result = provider.test_game(
                    game,
                    spins=1,
                    timeout_s=5,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

            self.assertEqual(result.status, "OK")
            self.assertEqual(result.successful_spins, 2)
            purchase = next(
                mode for mode in result.discovered_modes
                if mode["id"] == "PURCHASE_BONUS_BUY"
            )
            self.assertEqual(purchase["cost_multiplier"], 50.0)
            self.assertEqual(purchase["cost_source"], "observed_balance_delta")
            self.assertEqual(
                purchase["discovery_state"],
                "SERVER_ADVERTISED_PROBE",
            )
            self.assertTrue(purchase["executable"])

    def test_level_selector_drives_effective_bet_and_purchase_level(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = BGamingProvider(Path(temp))
            game = Game(
                provider="bgaming",
                slug="level-selector-slot",
                name="Level Selector Slot",
                url="https://demo.bgaming-network.com/games/LevelSelector/FUN",
            )
            session = requests.Session()
            runtime = BGamingRuntime(
                session=session,
                launch_url="https://demo.bgaming-network.com/games/LevelSelector/FUN",
                api_url="https://demo.bgaming-network.com/api/LevelSelector/1/session",
                identifier="LevelSelector",
                csrf_header_name="X-CSRF-Token",
                csrf_header_value="secret",
                options={},
                round_series_id=1,
            )
            init = {
                "api_version": "2",
                "options": {
                    "default_bet": 1,
                    "layout": {"reels": 5, "rows": 3},
                    "feature_options": {
                        "feature_multipliers": {
                            "freespin_chance": {"5": 150}
                        },
                        "disabled_features": [],
                    },
                },
                "balance": {"wallet": 100000, "game": 0},
                "flow": {
                    "command": "init",
                    "state": "ready",
                    "available_actions": ["init", "spin"],
                },
            }
            base_spin = {
                "api_version": "2",
                "outcome": {
                    "screen": [["1", "2", "3"]] * 5,
                    "bet": 88,
                    "win": 0,
                },
                "balance": {"wallet": 99912, "game": 0},
                "flow": {
                    "round_id": 1,
                    "last_action_id": "1_1",
                    "command": "spin",
                    "state": "closed",
                    "available_actions": ["init", "spin"],
                    "purchased_feature": {},
                },
            }
            purchase_spin = {
                "api_version": "2",
                "outcome": {
                    "screen": [["1", "2", "3"]] * 5,
                    "bet": 88,
                    "win": 0,
                },
                "balance": {"wallet": 99868, "game": 0},
                "flow": {
                    "round_id": 2,
                    "last_action_id": "2_1",
                    "command": "spin",
                    "state": "closed",
                    "available_actions": ["init", "spin"],
                    "purchased_feature": {
                        "name": "freespin_chance",
                        "level": "5",
                    },
                },
            }
            sent_options = []

            def fake_post(_runtime, command, **kwargs):
                if command == "init":
                    return Response(), {"command": "init"}, init
                options = dict(kwargs.get("options") or {})
                sent_options.append(options)
                payload = {"command": command, "options": options}
                if options.get("purchased_feature"):
                    return Response(), payload, purchase_spin
                return Response(), payload, base_spin

            profile = BGamingProfile(
                family=API_V2,
                confidence=1.0,
                spin_options={"gold_symbols_count": "5"},
                spin_option_choices={"gold_symbols_count": ["1", "5"]},
                effective_bet_selector="gold_symbols_count",
                effective_bet_multipliers={"1": 8.0, "5": 88.0},
                dynamic_purchased_feature=True,
                purchase_feature_level_supported=True,
                purchase_features=["freespin_chance"],
                source="bundle",
            )

            with (
                patch.object(provider, "_new_session", return_value=session),
                patch(
                    "tester_spin.providers.bgaming.execution.bootstrap_game",
                    return_value=runtime,
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.discover_profile",
                    return_value=profile,
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.discover_api_v2_wire_profile",
                    return_value={},
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.post_command",
                    side_effect=fake_post,
                ),
            ):
                result = provider.test_game(
                    game,
                    spins=1,
                    timeout_s=5,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

            self.assertEqual(result.status, "OK")
            self.assertEqual(result.successful_spins, 2)
            self.assertEqual(
                sent_options[0],
                {"bet": 1, "gold_symbols_count": "5"},
            )
            self.assertEqual(
                sent_options[1],
                {
                    "bet": 1,
                    "gold_symbols_count": "5",
                    "purchased_feature": "freespin_chance",
                    "purchased_feature_level": "5",
                },
            )
            purchase = next(
                mode for mode in result.discovered_modes
                if mode["id"] == "PURCHASE_FREESPIN_CHANCE_LEVEL_5"
            )
            self.assertEqual(purchase["cost_multiplier"], 1.5)

    def test_tiered_purchase_uses_matching_client_proven_row_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = BGamingProvider(Path(temp))
            game = Game(
                provider="bgaming",
                slug="tiered-row-slot",
                name="Tiered Row Slot",
                url="https://demo.bgaming-network.com/games/TieredRows/FUN",
            )
            session = requests.Session()
            runtime = BGamingRuntime(
                session=session,
                launch_url="https://demo.bgaming-network.com/games/TieredRows/FUN",
                api_url="https://demo.bgaming-network.com/api/TieredRows/1/session",
                identifier="TieredRows",
                csrf_header_name="X-CSRF-Token",
                csrf_header_value="secret",
                options={},
                round_series_id=1,
            )
            init = {
                "api_version": "2",
                "options": {
                    "default_bet": 5,
                    "layout": {"reels": 5, "rows": 5},
                    "valid_bets": {
                        "3": [5, 10, 20],
                        "4": [5, 10, 20],
                        "5": [5, 10, 20],
                    },
                    "feature_options": {
                        "feature_multipliers": {
                            "freespin_buy": {"3": 12000}
                        },
                        "disabled_features": [],
                    },
                },
                "balance": {"wallet": 100000, "game": 0},
                "flow": {
                    "command": "init",
                    "state": "ready",
                    "available_actions": ["init", "spin"],
                },
            }
            base_spin = {
                "api_version": "2",
                "outcome": {
                    "screen": [["1", "2", "3", "4", "5"]] * 5,
                    "bet": 20,
                    "win": 0,
                },
                "balance": {"wallet": 99980, "game": 0},
                "flow": {
                    "round_id": 1,
                    "last_action_id": "1_1",
                    "command": "spin",
                    "state": "closed",
                    "available_actions": ["init", "spin"],
                    "purchased_feature": {},
                },
            }
            purchase_spin = {
                "api_version": "2",
                "outcome": {
                    "screen": [["1", "2", "3"]] * 5,
                    "bet": 20,
                    "win": 0,
                },
                "balance": {"wallet": 88000, "game": 0},
                "flow": {
                    "round_id": 2,
                    "last_action_id": "2_1",
                    "command": "spin",
                    "state": "closed",
                    "available_actions": ["init", "spin"],
                    "purchased_feature": {
                        "name": "freespin_buy",
                        "level": "3",
                    },
                },
            }
            sent_options = []

            def fake_post(_runtime, command, **kwargs):
                if command == "init":
                    return Response(), {"command": "init"}, init
                options = dict(kwargs.get("options") or {})
                sent_options.append(options)
                payload = {"command": command, "options": options}
                if options.get("purchased_feature"):
                    if str(options.get("rows")) != str(
                        options.get("purchased_feature_level")
                    ):
                        response = requests.Response()
                        response.status_code = 422
                        response.headers["Content-Type"] = "application/json"
                        response._content = (
                            b'{"errors":[{"code":203,"desc":"invalid_options"}]}'
                        )
                        response.request = requests.Request(
                            "POST",
                            runtime.api_url,
                        ).prepare()
                        raise requests.HTTPError(
                            "422 invalid_options",
                            response=response,
                        )
                    return Response(), payload, purchase_spin
                return Response(), payload, base_spin

            profile = BGamingProfile(
                family=API_V2,
                confidence=1.0,
                evidence=[
                    "client.additionalSpinOptions.rows",
                    "init.valid_bets:rows-domain",
                ],
                spin_options={"rows": 5},
                spin_option_choices={"rows": [3, 4, 5]},
                dynamic_purchased_feature=True,
                purchase_feature_level_supported=True,
                purchase_features=["freespin_buy"],
                source="bundle",
            )

            with (
                patch.object(provider, "_new_session", return_value=session),
                patch(
                    "tester_spin.providers.bgaming.execution.bootstrap_game",
                    return_value=runtime,
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.discover_profile",
                    return_value=profile,
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.discover_api_v2_wire_profile",
                    return_value={},
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.post_command",
                    side_effect=fake_post,
                ),
            ):
                result = provider.test_game(
                    game,
                    spins=1,
                    timeout_s=5,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

            self.assertEqual(result.status, "OK")
            self.assertEqual(result.successful_spins, 2)
            purchase_options = next(
                options for options in sent_options
                if options.get("purchased_feature") == "freespin_buy"
            )
            self.assertEqual(purchase_options["rows"], 3)
            self.assertEqual(
                purchase_options["purchased_feature_level"],
                "3",
            )

    def test_unexplained_bet_translation_cannot_be_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = BGamingProvider(Path(temp))
            game = Game(
                provider="bgaming",
                slug="generic-slot",
                name="Generic Slot",
                url="https://demo.bgaming-network.com/play/GenericSlot/FUN?server=demo",
            )
            session = requests.Session()
            runtime = BGamingRuntime(
                session=session,
                launch_url="https://demo.bgaming-network.com/games/GenericSlot/FUN",
                api_url="https://demo.bgaming-network.com/api/GenericSlot/1/session",
                identifier="GenericSlot",
                csrf_header_name="X-CSRF-Token",
                csrf_header_value="secret",
                options={},
                round_series_id=1,
            )
            init = {
                "api_version": "2",
                "options": {
                    "default_bet": 100,
                    "layout": {"reels": 5, "rows": 3},
                },
                "balance": {"wallet": 100000, "game": 0},
                "flow": {
                    "command": "init",
                    "state": "ready",
                    "available_actions": ["init", "spin"],
                },
            }
            translated = {
                "api_version": "2",
                "outcome": {
                    "screen": [["1", "2", "3"]] * 5,
                    "bet": 400,
                    "win": 0,
                },
                "balance": {"wallet": 99600, "game": 0},
                "flow": {
                    "round_id": 1,
                    "last_action_id": "1_1",
                    "command": "spin",
                    "state": "closed",
                    "available_actions": ["init", "spin"],
                },
            }

            def fake_post(_runtime, command, **kwargs):
                if command == "init":
                    return Response(), {"command": "init"}, init
                return Response(), {"command": command}, translated

            with (
                patch.object(provider, "_new_session", return_value=session),
                patch(
                    "tester_spin.providers.bgaming.execution.bootstrap_game",
                    return_value=runtime,
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.discover_profile",
                    return_value=BGamingProfile(
                        family=API_V2,
                        confidence=1.0,
                        source="init",
                    ),
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.post_command",
                    side_effect=fake_post,
                ),
            ):
                result = provider.test_game(
                    game,
                    spins=1,
                    timeout_s=5,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

            self.assertEqual(result.status, "PARCIAL")
            self.assertEqual(result.successful_spins, 0)
            self.assertFalse(result.attempts[0].ok)
            self.assertIn("bet devuelta=400", result.attempts[0].warning)


if __name__ == "__main__":
    unittest.main()
