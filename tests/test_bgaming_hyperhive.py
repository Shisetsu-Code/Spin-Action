from __future__ import annotations

import unittest

import requests

from tester_spin.providers.bgaming.hyperhive import (
    _result_summary,
    is_hyperhive_runtime,
)
from tester_spin.providers.bgaming.runtime import BGamingRuntime, validate_spin


class BGamingHyperHiveTests(unittest.TestCase):
    def test_detects_hyperhive_from_launch_path(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://blackbeards-bounty.demo.bgaming-network.com/hyperhive?launch_token=x",
            api_url="https://blackbeards-bounty.demo.bgaming-network.com/api/BlackbeardsBounty/4365404/session",
            identifier="BlackbeardsBounty",
            csrf_header_name="X-CSRF-Token",
            csrf_header_value="secret",
            options={
                "game": "slots/blackbeards_bounty",
                "version": "1.0.0",
                "game_bundle_source": "https://example.test/bundle.js",
            },
            round_series_id=1,
        )
        self.assertTrue(is_hyperhive_runtime(runtime))

    def test_hyperhive_result_summary_uses_final_balance_and_round_step(self) -> None:
        data = {
            "id": "rpc-id",
            "jsonrpc": "2.0",
            "result": {
                "final": True,
                "balance": 115680,
                "resp": {
                    "bet": 100,
                    "roundStep": 7,
                    "freespins": 7,
                    "totalWin": 35930,
                    "commonGame": {
                        "data": [
                            {
                                "type": "INITIAL_SPIN",
                                "table": [["L1", "L2", "L3"]],
                            }
                        ]
                    },
                },
            },
        }
        summary = _result_summary(data)
        self.assertTrue(summary["final"])
        self.assertEqual(summary["balance"], 115680)
        self.assertEqual(summary["round_step"], 7)
        self.assertEqual(summary["freespins"], 7)
        self.assertEqual(summary["total_win"], 35930)
        self.assertEqual(len(summary["response_sha256"]), 16)

    def test_freespin_does_not_require_outcome_bet_to_equal_base_bet(self) -> None:
        data = {
            "api_version": "2",
            "outcome": {
                "screen": [
                    ["1", "2", "3"],
                    ["1", "2", "3"],
                    ["1", "2", "3"],
                    ["1", "2", "3"],
                    ["1", "2", "3"],
                ],
                "bet": 0,
                "win": 40,
            },
            "balance": {"wallet": 99900, "game": 40},
            "flow": {
                "state": "closed",
                "command": "freespin",
                "available_actions": ["init", "spin"],
            },
        }
        warnings = validate_spin(
            data,
            requested_bet=100,
            previous_balance_total=99900,
            expected_reels=5,
            expected_rows=3,
            command="freespin",
            expected_debit=0,
        )
        self.assertEqual(warnings, [])

    def test_dynamic_layout_accepts_variable_reel_heights(self) -> None:
        data = {
            "api_version": "2",
            "outcome": {
                "screen": [
                    ["1", "2", "3"],
                    ["1", "2", "3", "4"],
                    ["1", "2", "3", "4", "5"],
                    ["1", "2", "3"],
                    ["1", "2", "3", "4"],
                    ["1", "2"],
                ],
                "bet": 20,
                "win": 0,
            },
            "balance": {"wallet": 99980, "game": 0},
            "flow": {
                "state": "closed",
                "command": "spin",
                "available_actions": ["init", "spin"],
            },
        }
        warnings = validate_spin(
            data,
            requested_bet=20,
            previous_balance_total=100000,
            expected_reels=6,
            expected_rows=8,
            command="spin",
            expected_debit=20,
            variable_layout=True,
        )
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
