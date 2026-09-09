from __future__ import annotations

import unittest

from tester_spin.providers.bgaming.runtime import (
    balance_total,
    extract_options,
    sanitize_options,
    sanitize_session_url,
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
        self.assertTrue(any("select_bonus" in warning for warning in warnings))
        self.assertTrue(any("balance inconsistente" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
