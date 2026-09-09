from __future__ import annotations

import unittest

from tester_spin.providers.bgaming.runtime import (
    balance_total,
    build_line_bets,
    discover_purchase_modes,
    extract_options,
    flow_continuation_command,
    is_line_bet_init,
    line_bet_count,
    pending_flow_actions,
    preselection_multiplier,
    purchase_expected_debit,
    purchase_names_equivalent,
    resolve_base_bet,
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

    def test_big_atlantis_percent_basis_feature_multipliers(self) -> None:
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
        self.assertEqual(by_name["freespin_chance"]["base_multiplier"], 100)
        self.assertEqual(
            by_name["freespin_chance"]["base_source"],
            "implicit_percent_basis",
        )
        self.assertEqual(
            by_name["freespin_chance"]["cost_multiplier"],
            2.0,
        )
        self.assertEqual(
            by_name["freespin_buy"]["cost_multiplier"],
            80.0,
        )
        self.assertEqual(
            purchase_expected_debit(30, by_name["freespin_chance"]),
            60.0,
        )
        self.assertEqual(
            purchase_expected_debit(30, by_name["freespin_buy"]),
            2400.0,
        )

    def test_base_spin_can_use_server_reported_effective_bet(self) -> None:
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
        self.assertEqual(
            validate_spin(
                response,
                requested_bet=100,
                previous_balance_total=100000,
                expected_reels=5,
                expected_rows=5,
                command="spin",
                expected_debit=100,
                trust_returned_bet=True,
            ),
            [],
        )

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
