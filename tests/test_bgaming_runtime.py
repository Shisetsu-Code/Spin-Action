from __future__ import annotations

import unittest

from tester_spin.providers.bgaming.runtime import (
    balance_total,
    discover_purchase_modes,
    extract_options,
    pending_flow_actions,
    purchase_expected_debit,
    resolve_base_bet,
    sanitize_error_text,
    sanitize_options,
    sanitize_session_url,
    spin_remote_proof,
    validate_init,
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
