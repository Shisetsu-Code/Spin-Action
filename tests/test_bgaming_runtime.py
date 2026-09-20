from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from tester_spin.providers.bgaming.runtime import (
    BGamingRuntime,
    balance_total,
    build_line_bets,
    discover_additional_spin_option_choices,
    discover_api_v2_wire_profile,
    discover_client_extra_data_defaults,
    discover_effective_bet_multipliers,
    discover_purchase_modes,
    effective_bet_for_options,
    extract_options,
    extract_script_urls,
    flow_continuation_command,
    infer_missing_wire_options,
    infer_observed_debit,
    is_line_bet_init,
    legacy_safe_terminal_command,
    line_bet_count,
    pending_flow_actions,
    post_command,
    preselection_multiplier,
    purchase_expected_debit,
    purchase_names_equivalent,
    resolve_base_bet,
    resolve_fresh_demo_url,
    runtime_shape_summary,
    sanitize_error_text,
    sanitize_options,
    sanitize_session_url,
    spin_remote_proof,
    validate_init,
    validate_line_spin,
    validate_spin,
)


class BGamingRuntimeTests(unittest.TestCase):
    def test_extracts_window_options_without_executing_javascript(self) -> None:
        html = """
        <html><head><script>
        window.__OPTIONS__ = {
          "identifier":"TreasureOfAnubis",
          "api":"https://demo.bgaming-network.com/api/TreasureOfAnubis/2367150/session-token",
          "csrfTokenHeaderName":"X-CSRF-Token",
          "csrfTokenHeaderValue":"secret"
        };
        </script></head></html>
        """
        options = extract_options(html)
        self.assertEqual(options["identifier"], "TreasureOfAnubis")
        self.assertEqual(options["csrfTokenHeaderName"], "X-CSRF-Token")

    def test_unknown_runtime_shape_summary_contains_no_values(self) -> None:
        summary = runtime_shape_summary(
            {
                "token": "secret",
                "wallet": 100000,
                "game": {"state": "mystery", "secret": "value"},
                "available_commands": ["init", "play"],
            }
        )
        self.assertIn("token", summary["top_level_keys"])
        self.assertEqual(summary["top_level_types"]["wallet"], "int")
        self.assertEqual(summary["game_keys"], ["secret", "state"])
        self.assertEqual(summary["available_commands"], ["init", "play"])
        self.assertNotIn("secret", str(summary).replace("'secret'", ""))
        self.assertNotIn("100000", str(summary))

    def test_extracts_actual_launch_script_urls(self) -> None:
        html = """
        <html><head>
          <script src="/assets/runtime-a1.js"></script>
          <script src="https://cdn.example/game-b2.js"></script>
        </head></html>
        """
        self.assertEqual(
            extract_script_urls(html, "https://demo.example/hyperhive"),
            [
                "https://demo.example/assets/runtime-a1.js",
                "https://cdn.example/game-b2.js",
            ],
        )

    def test_discovers_literal_dynamic_spin_choices(self) -> None:
        bundle = (
            'setCurrentVolatility(t){'
            'this.additionalSpinOptions.volatility='
            '1==this.getCurrentVolatility()?"low":"medium"}'
        )
        self.assertEqual(
            discover_additional_spin_option_choices(bundle),
            {"volatility": ["low", "medium"]},
        )

    def test_discovers_numeric_choices_from_dynamic_setter_calls(self) -> None:
        bundle = (
            'class X{setLevel(t,e){'
            'this.additionalSpinOptions.gold_symbols_count=""+t}}'
            'currentScene.setLevel`1;'
            'currentScene.setLevel`2;'
            'currentScene.setLevel`5;'
        )
        self.assertEqual(
            discover_additional_spin_option_choices(bundle),
            {"gold_symbols_count": ["1", "2", "5"]},
        )

    def test_discovers_unambiguous_effective_bet_level_table(self) -> None:
        bundle = (
            'var A={1:8,2:18,3:38,4:68,5:88};'
            'class X{BET_BY_SPECIAL_LVL=A;}'
        )
        self.assertEqual(
            discover_effective_bet_multipliers(bundle),
            {
                "1": 8.0,
                "2": 18.0,
                "3": 38.0,
                "4": 68.0,
                "5": 88.0,
            },
        )
        self.assertEqual(
            effective_bet_for_options(
                1,
                selector_field="gold_symbols_count",
                multipliers={"1": 8.0, "5": 88.0},
                options={"gold_symbols_count": "5"},
            ),
            88.0,
        )

    def test_discovers_tiered_purchase_modes(self) -> None:
        modes = discover_purchase_modes(
            {
                "options": {
                    "feature_options": {
                        "feature_multipliers": {
                            "base_bet": 10,
                            "freespin_buy": {
                                "1": 750,
                                "2": 1500,
                            },
                        },
                        "disabled_features": [],
                    }
                }
            }
        )
        self.assertEqual(
            [
                (mode["name"], mode["level"], mode["cost_multiplier"])
                for mode in modes
            ],
            [
                ("freespin_buy", "1", 75.0),
                ("freespin_buy", "2", 150.0),
            ],
        )

    def test_expected_effective_bet_can_explain_server_bet_translation(self) -> None:
        warnings = validate_spin(
            {
                "api_version": "2",
                "outcome": {
                    "screen": [["1"]] * 5,
                    "bet": 88,
                    "win": 0,
                },
                "balance": {"wallet": 99912, "game": 0},
                "flow": {
                    "command": "spin",
                    "state": "closed",
                    "available_actions": ["init", "spin"],
                },
            },
            requested_bet=1,
            expected_outcome_bet=88,
            previous_balance_total=100000,
            expected_reels=5,
            expected_rows=3,
            expected_debit=88,
        )
        self.assertEqual(warnings, [])

    def test_client_extra_data_defaults_are_extracted_without_eval(self) -> None:
        bundle = (
            'class X{setExtraDataOptions(){'
            'this.extraDataOptions={extra_data:{api_version:2,flag:true,'
            'label:"v2",token:"secret",dynamic:window.value}}'
            '}}'
        )
        self.assertEqual(
            discover_client_extra_data_defaults(bundle),
            {
                "api_version": 2,
                "flag": True,
                "label": "v2",
            },
        )

    def test_post_command_merges_runtime_extra_data_defaults(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://demo.bgaming-network.com/games/Example/FUN",
            api_url="https://demo.bgaming-network.com/api/Example/1/session",
            identifier="Example",
            csrf_header_name="X-CSRF",
            csrf_header_value="secret",
            options={},
            round_series_id=12345,
            request_extra_data={"api_version": 2},
        )

        class Response:
            status_code = 200
            text = "{}"

            def raise_for_status(self) -> None:
                return None

            def json(self):
                return {}

        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs["json"]
            return Response()

        with patch.object(runtime.session, "post", side_effect=fake_post):
            _response, payload, _data = post_command(
                runtime,
                "spin",
                timeout_s=1,
                options={"bet": 90},
                extra_data={"client_marker": 7},
            )

        self.assertEqual(
            payload["extra_data"],
            {
                "round_series_id": 12345,
                "api_version": 2,
                "client_marker": 7,
            },
        )
        self.assertEqual(captured["json"], payload)

    def test_protocol_discovery_follows_wrapper_to_game_bundle(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://demo.bgaming-network.com/games/Generic/FUN",
            api_url="https://demo.bgaming-network.com/api/Generic/1/session",
            identifier="Generic",
            csrf_header_name="X-CSRF",
            csrf_header_value="secret",
            options={},
            round_series_id=1,
            script_urls=["https://boost2.bgaming-network.com/wrapper.js"],
        )

        class Response:
            status_code = 200
            headers = {}

            def __init__(self, text):
                self.text = text
                self.content = text.encode("utf-8")

            def raise_for_status(self) -> None:
                return None

        responses = {
            "https://boost2.bgaming-network.com/wrapper.js": Response(
                'var game="https://cdn.bgaming-network.com/html/Generic/v1/bundle.js";'
            ),
            "https://cdn.bgaming-network.com/html/Generic/v1/bundle.js": Response(
                'additionalSpinOptions.mode="60";'
                'purchased_feature:"bonus_buy";'
                'round_series_id'
            ),
        }

        def fake_get(url, **_kwargs):
            return responses[url]

        with patch.object(runtime.session, "get", side_effect=fake_get):
            profile = discover_api_v2_wire_profile(runtime, timeout_s=1)

        self.assertEqual(
            profile["source"],
            "https://cdn.bgaming-network.com/html/Generic/v1/bundle.js",
        )
        self.assertEqual(profile["purchase_features"], ["bonus_buy"])

    def test_protocol_discovery_resolves_vite_root_assets_under_resources_path(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://demo.bgaming-network.com/games/Generic/FUN",
            api_url="https://demo.bgaming-network.com/api/Generic/1/session",
            identifier="Generic",
            csrf_header_name="X-CSRF",
            csrf_header_value="secret",
            options={
                "resources_path": "https://cdn.bgaming-network.com/html/Generic",
                "games_loader_source": (
                    "https://cdn.bgaming-network.com/html/Generic/loader.js"
                ),
            },
            round_series_id=1,
        )

        class Response:
            headers = {}

            def __init__(self, text: str, status_code: int = 200):
                self.text = text
                self.status_code = status_code

            def raise_for_status(self) -> None:
                if self.status_code >= 400:
                    response = requests.Response()
                    response.status_code = self.status_code
                    raise requests.HTTPError(response=response)

        loader = "https://cdn.bgaming-network.com/html/Generic/loader.js"
        root_asset = "https://cdn.bgaming-network.com/assets/index-vite.js"
        scoped_asset = (
            "https://cdn.bgaming-network.com/html/Generic/assets/index-vite.js"
        )
        responses = {
            loader: Response('import("/assets/index-vite.js")'),
            root_asset: Response("", 404),
            scoped_asset: Response(
                'additionalSpinOptions.purchased_feature="bonus_buy";'
                'purchased_feature:"bonus_buy";round_series_id'
            ),
        }
        called: list[str] = []

        def fake_get(url, **_kwargs):
            called.append(url)
            return responses[url]

        with patch.object(runtime.session, "get", side_effect=fake_get):
            profile = discover_api_v2_wire_profile(runtime, timeout_s=1)

        self.assertIn(root_asset, called)
        self.assertIn(scoped_asset, called)
        self.assertEqual(profile["source"], scoped_asset)
        self.assertTrue(profile["dynamic_purchased_feature"])
        self.assertEqual(profile["purchase_features"], ["bonus_buy"])

    def test_protocol_discovery_ignores_gtag_and_uses_bgaming_bundle(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://demo.bgaming-network.com/games/Example/FUN",
            api_url="https://demo.bgaming-network.com/api/Example/1/session",
            identifier="Example",
            csrf_header_name="X-CSRF",
            csrf_header_value="secret",
            options={},
            round_series_id=1,
            script_urls=[
                "https://www.googletagmanager.com/gtag/js",
                "https://cdn.bgaming-network.com/html/Example/bundle.js",
            ],
        )

        class Response:
            text = (
                'this.additionalSpinOptions.rows=this.rows;'
                'purchased_feature:"bonus_buy";'
                'round_series_id'
            )
            status_code = 200
            headers = {}

            def raise_for_status(self) -> None:
                return None

        called: list[str] = []

        def fake_get(url, **_kwargs):
            called.append(url)
            return Response()

        with patch.object(runtime.session, "get", side_effect=fake_get):
            profile = discover_api_v2_wire_profile(runtime, timeout_s=1)

        self.assertEqual(
            called,
            ["https://cdn.bgaming-network.com/html/Example/bundle.js"],
        )
        self.assertEqual(
            profile["source"],
            "https://cdn.bgaming-network.com/html/Example/bundle.js",
        )
        self.assertEqual(profile["required_option_fields"], ["rows"])
        self.assertEqual(profile["purchase_features"], ["bonus_buy"])

    def test_legacy_optional_gamble_prefers_non_wagering_finish(self) -> None:
        payload = {
            "game": {"state": "check_card", "action": "spin"},
            "available_commands": ["check_card", "finish", "init"],
            "bets": {"lines": {"0": 1}},
            "balance": 100005,
        }
        self.assertEqual(legacy_safe_terminal_command(payload), "finish")
        warnings, _win = validate_line_spin(
            payload,
            requested_line_bet=1,
            line_count=1,
            previous_balance_total=100000,
            allow_safe_finish=True,
        )
        self.assertFalse(
            any("state no terminal" in warning for warning in warnings)
        )
        self.assertFalse(
            any("sin spin disponible" in warning for warning in warnings)
        )

    def test_sanitizes_session_tokens_and_csrf(self) -> None:
        clean = sanitize_options(
            {
                "play_token": "secret-play",
                "csrfTokenHeaderValue": "secret-csrf",
                "api": "https://demo.bgaming-network.com/api/TreasureOfAnubis/2367150/session-token",
                "websocket_url": "wss://demo.bgaming-network.com/cable?play_token=secret",
            }
        )
        self.assertEqual(clean["play_token"], "<redacted>")
        self.assertEqual(clean["csrfTokenHeaderValue"], "<redacted>")
        self.assertNotIn("session-token", clean["api"])
        self.assertNotIn("secret", clean["websocket_url"])
        self.assertEqual(
            sanitize_session_url(
                "https://demo.bgaming-network.com/games/TreasureOfAnubis/FUN?launch_token=secret"
            ),
            "https://demo.bgaming-network.com/games/TreasureOfAnubis/FUN",
        )

    def test_redacts_tokens_from_runtime_errors(self) -> None:
        message = sanitize_error_text(
            "403 https://demo.bgaming-network.com/api/TreasureOfAnubis/2367150/session-secret"
            "?play_token=secret-play"
        )
        self.assertNotIn("session-secret", message)
        self.assertNotIn("secret-play", message)
        self.assertIn("<session>", message)
        self.assertIn("<redacted>", message)

    def test_validates_observed_init_and_spin_contract(self) -> None:
        init = {
            "api_version": "2",
            "options": {
                "available_bets": [9, 18, 27, 45, 90],
                "default_bet": 90,
                "layout": {"reels": 5, "rows": 3},
                "currency": {"code": "FUN", "subunits": 100, "exponent": 2},
            },
            "balance": {"game": 0, "wallet": 100000},
            "flow": {
                "round_id": None,
                "last_action_id": None,
                "state": "ready",
                "command": "init",
                "available_actions": ["init", "spin"],
            },
        }
        spin = {
            "api_version": "2",
            "outcome": {
                "screen": [
                    ["0", "7", "4"],
                    ["4", "5", "1"],
                    ["4", "5", "6"],
                    ["8", "4", "0"],
                    ["7", "6", "8"],
                ],
                "bet": 90,
                "win": 40,
                "wins": [["line", 40, [0, 0, 0], 1]],
            },
            "balance": {"game": 40, "wallet": 99910},
            "flow": {
                "round_id": 17236767117,
                "last_action_id": "17236767117_1",
                "state": "closed",
                "command": "spin",
                "available_actions": ["init", "spin"],
            },
        }

        self.assertEqual(validate_init(init), [])
        self.assertEqual(balance_total(init), 100000)
        self.assertEqual(
            validate_spin(
                spin,
                requested_bet=90,
                previous_balance_total=100000,
                expected_reels=5,
                expected_rows=3,
            ),
            [],
        )

    def test_resolves_base_bet_from_available_bets_when_default_missing(self) -> None:
        init = {
            "options": {
                "available_bets": [500, 100, 200],
            }
        }
        bet, source = resolve_base_bet(init)
        self.assertEqual(bet, 100)
        self.assertEqual(source, "available_bets:min")
        self.assertEqual(validate_init({
            "api_version": "2",
            "options": {"available_bets": [500, 100, 200]},
            "flow": {
                "state": "ready",
                "command": "init",
                "available_actions": ["init", "spin"],
            },
        }), [])

    def test_optional_actions_are_coverage_not_base_spin_failures(self) -> None:
        spin = {
            "api_version": "2",
            "outcome": {
                "screen": [["1", "2", "3"]] * 5,
                "bet": 90,
                "win": 0,
                "wins": [],
            },
            "balance": {"game": 0, "wallet": 99910},
            "flow": {
                "state": "closed",
                "command": "spin",
                "available_actions": ["init", "spin", "buy_feature", "select_bonus"],
            },
        }
        self.assertEqual(
            validate_spin(
                spin,
                requested_bet=90,
                previous_balance_total=100000,
                expected_reels=5,
                expected_rows=3,
            ),
            [],
        )
        self.assertEqual(
            pending_flow_actions(spin),
            ["buy_feature", "select_bonus"],
        )

    def test_discovers_har_observed_purchase_modes_and_costs(self) -> None:
        init = {
            "options": {
                "feature_options": {
                    "feature_multipliers": {
                        "bonus_buy": 2000,
                        "bonus_chance": 30,
                        "base_bet": 20,
                    },
                    "disabled_features": [],
                }
            }
        }
        modes = discover_purchase_modes(init)
        self.assertEqual(
            [(mode["name"], mode["cost_multiplier"]) for mode in modes],
            [("bonus_buy", 100.0), ("bonus_chance", 1.5)],
        )
        self.assertEqual(purchase_expected_debit(200, modes[0]), 20000.0)
        self.assertEqual(purchase_expected_debit(200, modes[1]), 300.0)

    def test_accepts_alien_fruits_seeded_purchase_result(self) -> None:
        purchase = {
            "name": "bonus_buy",
            "feature_multiplier": 2000,
            "base_multiplier": 20,
            "cost_multiplier": 100.0,
        }
        response = {
            "api_version": "2",
            "outcome": {
                "screen": None,
                "special_symbols": None,
                "bet": 200,
                "win": 6950,
                "wins": [],
                "storage": {"seed": 77018, "mode": "1"},
            },
            "balance": {"game": 6950, "wallet": 80000},
            "flow": {
                "round_id": 17238408022,
                "last_action_id": "17238408022_1",
                "state": "closed",
                "command": "spin",
                "available_actions": ["init", "spin"],
                "purchased_feature": {"name": "bonus_buy"},
            },
        }
        self.assertEqual(
            validate_spin(
                response,
                requested_bet=200,
                previous_balance_total=100000,
                expected_reels=1,
                expected_rows=1,
                command="spin",
                expected_debit=purchase_expected_debit(200, purchase),
            ),
            [],
        )

    def test_validates_treasure_of_anubis_freespin_continuation(self) -> None:
        response = {
            "api_version": "2",
            "features": {
                "freespins_issued": 11,
                "freespins_left": 10,
            },
            "outcome": {
                "screen": [
                    ["1", "2", "3"],
                    ["1", "2", "3"],
                    ["1", "2", "3"],
                    ["1", "2", "3"],
                    ["1", "2", "3"],
                ],
                "bet": 90,
                "win": 0,
                "wins": [],
                "storage": {},
            },
            "balance": {"game": 0, "wallet": 100040},
            "flow": {
                "round_id": 17238373765,
                "last_action_id": "17238373765_2",
                "state": "freespins",
                "command": "freespin",
                "available_actions": ["init", "freespin"],
            },
        }
        self.assertEqual(
            validate_spin(
                response,
                requested_bet=90,
                previous_balance_total=100040,
                expected_reels=5,
                expected_rows=3,
                command="freespin",
                expected_debit=0,
            ),
            [],
        )
        self.assertEqual(pending_flow_actions(response), [])

    def test_validates_terminal_freespin_without_charging_bet(self) -> None:
        response = {
            "api_version": "2",
            "features": {
                "freespins_issued": 22,
                "freespins_left": 0,
            },
            "outcome": {
                "screen": [
                    ["1", "2", "3"],
                    ["1", "2", "3"],
                    ["1", "2", "3"],
                    ["1", "2", "3"],
                    ["1", "2", "3"],
                ],
                "bet": 90,
                "win": 0,
                "wins": [],
                "storage": {},
            },
            "balance": {"game": 3090, "wallet": 100040},
            "flow": {
                "round_id": 17238373765,
                "last_action_id": "17238373765_23",
                "state": "closed",
                "command": "freespin",
                "available_actions": ["init", "spin"],
            },
        }
        self.assertEqual(
            validate_spin(
                response,
                requested_bet=90,
                previous_balance_total=103130,
                expected_reels=5,
                expected_rows=3,
                command="freespin",
                expected_debit=0,
            ),
            [],
        )

    def test_always_up_purchase_enters_preselection_without_false_partial(self) -> None:
        purchase_spin = {
            "api_version": "2",
            "outcome": {
                "screen": [
                    ["2", "0", "8"],
                    ["5", "8", "5"],
                    ["8", "3", "3"],
                ],
                "bet": 200,
                "win": 0,
                "wins": [],
                "storage": None,
            },
            "balance": {"game": 0, "wallet": 83400},
            "flow": {
                "round_id": 17238938227,
                "last_action_id": "17238938227_1",
                "state": "preselection_game",
                "command": "spin",
                "available_actions": ["init", "preselection_game"],
                "purchased_feature": {"name": "bonus_buy"},
            },
        }
        self.assertEqual(
            validate_spin(
                purchase_spin,
                requested_bet=200,
                previous_balance_total=99400,
                expected_reels=3,
                expected_rows=3,
                command="spin",
                expected_debit=16000,
            ),
            [],
        )
        self.assertEqual(pending_flow_actions(purchase_spin), [])

    def test_always_up_preselection_reveal_is_zero_debit_terminal(self) -> None:
        reveal = {
            "api_version": "2",
            "features": {
                "bonus_data": {
                    "multiplier": 100,
                }
            },
            "outcome": {
                "screen": [
                    ["2", "0", "8"],
                    ["5", "8", "5"],
                    ["8", "3", "3"],
                ],
                "bet": 200,
                "win": 20000,
                "wins": [],
                "storage": None,
            },
            "balance": {"game": 20000, "wallet": 83400},
            "flow": {
                "round_id": 17238938227,
                "last_action_id": "17238938227_2",
                "state": "closed",
                "command": "preselection_game",
                "available_actions": ["init", "spin"],
                "purchased_feature": {"name": "bonus_buy"},
            },
        }
        self.assertEqual(preselection_multiplier(reveal), 100)
        self.assertEqual(
            validate_spin(
                reveal,
                requested_bet=200,
                previous_balance_total=83400,
                expected_reels=3,
                expected_rows=3,
                command="preselection_game",
                expected_debit=0,
            ),
            [],
        )
        self.assertEqual(pending_flow_actions(reveal), [])

    def test_deep_sea_legacy_line_bet_contract(self) -> None:
        init = {
            "options": {
                "line_bets": [10, 25, 50, 75, 100],
                "default_bet": 10,
                "lines": [[1, 1, 1, 1, 1] for _ in range(15)],
            },
            "balance": 100000,
            "available_commands": ["init", "spin"],
        }
        self.assertTrue(is_line_bet_init(init))
        self.assertEqual(line_bet_count(init), 15)
        self.assertEqual(validate_init(init), [])
        self.assertEqual(
            build_line_bets(init, 10),
            {str(index): 10 for index in range(15)},
        )

        spin = {
            "bets": {
                "lines": {str(index): 10 for index in range(15)}
            },
            "game": {
                "state": "closed",
                "action": "spin",
                "span_indices": [85, 188, 176, 77, 34],
            },
            "balance": 99850,
            "available_commands": ["init", "spin"],
            "extra_data": {
                "provable_data": [
                    {"hash": "abc"},
                    {"client_seed": "43533"},
                ]
            },
        }
        warnings, inferred_win = validate_line_spin(
            spin,
            requested_line_bet=10,
            line_count=15,
            previous_balance_total=100000,
        )
        self.assertEqual(warnings, [])
        self.assertEqual(inferred_win, 0)

    def test_line_bet_infers_win_from_balance_delta(self) -> None:
        spin = {
            "bets": {
                "lines": {str(index): 10 for index in range(15)}
            },
            "game": {"state": "closed", "action": "spin"},
            "balance": 99900,
            "available_commands": ["init", "spin"],
        }
        warnings, inferred_win = validate_line_spin(
            spin,
            requested_line_bet=10,
            line_count=15,
            previous_balance_total=100000,
        )
        self.assertEqual(warnings, [])
        self.assertEqual(inferred_win, 50)

    def test_server_advertised_respin_is_a_valid_continuation(self) -> None:
        spin = {
            "api_version": "2",
            "outcome": {
                "screen": [["1", "2", "3"]] * 5,
                "bet": 100,
                "win": 0,
            },
            "balance": {"wallet": 96000, "game": 0},
            "flow": {
                "state": "respin",
                "command": "spin",
                "available_actions": ["init", "respin"],
            },
        }
        self.assertEqual(flow_continuation_command(spin), "respin")
        self.assertEqual(pending_flow_actions(spin), [])
        self.assertEqual(
            validate_spin(
                spin,
                requested_bet=100,
                previous_balance_total=100000,
                expected_reels=5,
                expected_rows=3,
                command="spin",
                expected_debit=4000,
            ),
            [],
        )

    def test_server_advertised_play_bonus_is_a_valid_continuation(self) -> None:
        purchase = {
            "api_version": "2",
            "outcome": {
                "screen": [["1", "2", "3"]] * 3,
                "bet": 200,
                "win": 0,
            },
            "balance": {"wallet": 84000, "game": 0},
            "flow": {
                "state": "play_bonus",
                "command": "spin",
                "available_actions": ["init", "play_bonus"],
            },
        }
        self.assertEqual(flow_continuation_command(purchase), "play_bonus")
        self.assertEqual(pending_flow_actions(purchase), [])
        self.assertEqual(
            validate_spin(
                purchase,
                requested_bet=200,
                previous_balance_total=100000,
                expected_reels=3,
                expected_rows=3,
                command="spin",
                expected_debit=16000,
            ),
            [],
        )

    def test_purchase_variant_name_can_normalize_to_base_feature(self) -> None:
        self.assertTrue(
            purchase_names_equivalent("bonus_buy_0_chance", "bonus_buy")
        )
        self.assertTrue(
            purchase_names_equivalent("bonus_buy_1_chance", "bonus_buy")
        )
        self.assertFalse(
            purchase_names_equivalent("freespin_buy", "bonus_buy")
        )

    def test_preselection_direct_command_uses_server_advertised_state_command(self) -> None:
        response = {
            "flow": {
                "state": "preselection_game",
                "available_actions": ["init", "preselection_game"],
            }
        }
        self.assertEqual(
            flow_continuation_command(response),
            "preselection_game",
        )
        self.assertEqual(pending_flow_actions(response), [])

    def test_preselection_alias_uses_server_advertised_play_command(self) -> None:
        response = {
            "flow": {
                "state": "preselection_game",
                "available_actions": ["init", "play_preselection_game"],
            }
        }
        self.assertEqual(
            flow_continuation_command(response),
            "play_preselection_game",
        )
        self.assertEqual(pending_flow_actions(response), [])

    def test_freespin_can_transition_to_respin_without_false_warning(self) -> None:
        response = {
            "api_version": "2",
            "outcome": {
                "screen": None,
                "bet": 0,
                "win": 4200,
                "storage": None,
            },
            "balance": {"wallet": 96340, "game": 0},
            "flow": {
                "state": "respin",
                "command": "freespin",
                "available_actions": ["init", "respin"],
            },
        }
        self.assertEqual(flow_continuation_command(response), "respin")
        self.assertEqual(
            validate_spin(
                response,
                requested_bet=100,
                previous_balance_total=92140,
                expected_reels=5,
                expected_rows=3,
                command="freespin",
                expected_debit=0,
            ),
            [],
        )

    def test_balance_only_freespin_continuation_does_not_require_screen(self) -> None:
        response = {
            "api_version": "2",
            "outcome": {
                "screen": None,
                "bet": 0,
                "win": 120,
                "storage": None,
            },
            "balance": {"wallet": 95980, "game": 120},
            "flow": {
                "state": "freespins",
                "command": "freespin",
                "available_actions": ["init", "freespin"],
            },
        }
        self.assertEqual(
            validate_spin(
                response,
                requested_bet=60,
                previous_balance_total=95980,
                expected_reels=5,
                expected_rows=3,
                command="freespin",
                expected_debit=0,
            ),
            [],
        )

    def test_unreported_purchase_scale_is_not_hardcoded(self) -> None:
        init = {
            "options": {
                "default_bet": 30,
                "layout": {"reels": 5, "rows": 5},
                "feature_options": {
                    "feature_multipliers": {
                        "freespin_chance": 200,
                        "freespin_buy": 8000,
                    },
                    "disabled_features": [],
                },
            }
        }
        modes = discover_purchase_modes(init)
        by_name = {mode["name"]: mode for mode in modes}
        self.assertIsNone(by_name["freespin_chance"]["base_multiplier"])
        self.assertEqual(
            by_name["freespin_chance"]["base_source"],
            "unreported;learn-from-balance",
        )
        self.assertIsNone(by_name["freespin_chance"]["cost_multiplier"])
        self.assertIsNone(by_name["freespin_buy"]["cost_multiplier"])
        self.assertIsNone(
            purchase_expected_debit(30, by_name["freespin_chance"])
        )

        response = {
            "outcome": {"bet": 30, "win": 0},
            "balance": {"wallet": 9940, "game": 0},
        }
        self.assertEqual(
            infer_observed_debit(response, 10000),
            60.0,
        )

    def test_base_spin_rejects_unexplained_effective_bet_change(self) -> None:
        response = {
            "api_version": "2",
            "outcome": {
                "screen": [["1", "2", "3"]] * 5,
                "bet": 400,
                "win": 0,
            },
            "balance": {"wallet": 99600, "game": 0},
            "flow": {
                "state": "closed",
                "command": "spin",
                "available_actions": ["init", "spin"],
            },
        }
        warnings = validate_spin(
            response,
            requested_bet=100,
            previous_balance_total=100000,
            expected_reels=5,
            expected_rows=5,
            command="spin",
            expected_debit=100,
        )
        self.assertTrue(any("bet devuelta=400" in warning for warning in warnings))
        self.assertTrue(any("balance inconsistente" in warning for warning in warnings))

    def test_burning_chilli_bundle_discovers_mode_60(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://demo.bgaming-network.com/games/BurningChilliX/FUN",
            api_url="https://demo.bgaming-network.com/api/BurningChilliX/1/session",
            identifier="BurningChilliX",
            csrf_header_name="X-CSRF-Token",
            csrf_header_value="secret",
            options={
                "game_bundle_source": "https://example.test/bundle.js",
                "resources_path": "https://example.test",
            },
            round_series_id=1,
        )
        bundle = (
            'this.linesCount=this.linesCount||"60";'
            'this.additionalSpinOptions.mode=this.linesCount;'
        )

        class Response:
            text = bundle
            def raise_for_status(self) -> None:
                return None

        with patch(
            "tester_spin.providers.bgaming.runtime._runtime_bundle_candidates",
            return_value=["https://example.test/bundle.js"],
        ), patch.object(runtime.session, "get", return_value=Response()):
            profile = discover_api_v2_wire_profile(runtime, timeout_s=1)

        self.assertEqual(profile["spin_options"], {"mode": "60"})
        self.assertEqual(profile["kind"], "selectable-lines-mode")

    def test_422_infers_only_fields_named_by_server_validation(self) -> None:
        class Response:
            text = '{"error":{"rows":["is required"]}}'
            def json(self):
                return {"error": {"rows": ["is required"]}}

        inferred, evidence = infer_missing_wire_options(
            Response(),
            {
                "options": {
                    "default_bet": 100,
                    "layout": {"reels": 5, "rows": 3},
                    "currency": "FUN",
                }
            },
        )
        self.assertEqual(inferred, {"rows": 3})
        self.assertIn("rows", evidence)
        self.assertNotIn("reels", inferred)
        self.assertNotIn("currency", inferred)

    def test_resolves_ephemeral_demo_from_public_page_without_persisting_it(self) -> None:
        class PublicResponse:
            url = "https://bgaming.com/games/example"
            text = (
                '<a href="https://demo.bgaming-network.com/games/Example/FUN'
                '?play_token=ephemeral-secret">Play Demo</a>'
            )

            def raise_for_status(self) -> None:
                return None

        class DemoResponse:
            url = (
                "https://demo.bgaming-network.com/games/Example/FUN"
                "?play_token=ephemeral-secret"
            )
            text = """
            <script>
            window.__OPTIONS__ = {
              "identifier":"Example",
              "api":"https://demo.bgaming-network.com/api/Example/1/session",
              "csrfTokenHeaderName":"X-CSRF",
              "csrfTokenHeaderValue":"secret"
            };
            </script>
            """

            def raise_for_status(self) -> None:
                return None

        session = requests.Session()
        with patch.object(
            session,
            "get",
            side_effect=[PublicResponse(), DemoResponse()],
        ):
            resolved = resolve_fresh_demo_url(
                session,
                "https://bgaming.com/games/example",
                timeout_s=1,
            )
        self.assertIn("bgaming-network.com/games/Example/FUN", resolved)
        self.assertIn("play_token=ephemeral-secret", resolved)

    def test_unknown_advertised_state_is_not_auto_executed(self) -> None:
        payload = {
            "flow": {
                "state": "choose_bonus",
                "available_actions": ["init", "choose_bonus"],
            }
        }
        self.assertEqual(flow_continuation_command(payload), "")
        self.assertEqual(pending_flow_actions(payload), ["choose_bonus"])

    def test_remote_proof_changes_with_server_round_identity(self) -> None:
        first = {
            "outcome": {
                "screen": [["1", "2", "3"]] * 5,
                "bet": 90,
                "win": 0,
            },
            "balance": {"wallet": 99910, "game": 0},
            "flow": {
                "round_id": 1001,
                "last_action_id": "1001_1",
                "state": "closed",
                "command": "spin",
            },
        }
        second = {
            **first,
            "flow": {
                "round_id": 1002,
                "last_action_id": "1002_1",
                "state": "closed",
                "command": "spin",
            },
        }
        proof1 = spin_remote_proof(first)
        proof2 = spin_remote_proof(second)
        self.assertEqual(proof1["round_id"], 1001)
        self.assertEqual(proof1["last_action_id"], "1001_1")
        self.assertEqual(len(proof1["screen_sha256"]), 12)
        self.assertEqual(len(proof1["response_sha256"]), 16)
        self.assertNotEqual(proof1["response_sha256"], proof2["response_sha256"])

    def test_flags_balance_and_unknown_flow_without_inventing_handlers(self) -> None:
        spin = {
            "api_version": "2",
            "outcome": {
                "screen": [["1", "2", "3"]] * 5,
                "bet": 90,
                "win": 0,
                "wins": [],
            },
            "balance": {"game": 0, "wallet": 99999},
            "flow": {
                "state": "bonus",
                "command": "spin",
                "available_actions": ["init", "spin", "select_bonus"],
            },
        }
        warnings = validate_spin(
            spin,
            requested_bet=90,
            previous_balance_total=100000,
            expected_reels=5,
            expected_rows=3,
        )
        self.assertTrue(any("flow.state" in warning for warning in warnings))
        self.assertEqual(pending_flow_actions(spin), ["select_bonus"])
        self.assertTrue(any("balance inconsistente" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
