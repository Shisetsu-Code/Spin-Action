from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from tester_spin.providers.bgaming.hyperhive import (
    _result_summary,
    discover_modes_from_bundle,
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

    def test_does_not_misclassify_normal_game_bundle_as_hyperhive(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://demo.bgaming-network.com/games/BeastBand/FUN?launch_token=x",
            api_url="https://demo.bgaming-network.com/api/BeastBand/1/session",
            identifier="BeastBand",
            csrf_header_name="X-CSRF-Token",
            csrf_header_value="secret",
            options={
                "game": "slots/beast_band",
                "version": "1.0.0",
                "game_bundle_source": "https://example.test/bundle.js",
            },
            round_series_id=1,
        )
        self.assertFalse(is_hyperhive_runtime(runtime))

    def test_blazing_bundle_discovers_betting_actions_and_purchases(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://blazing-firepots.demo.bgaming-network.com/hyperhive",
            api_url="https://unused.example/api/session",
            identifier="BlazingFirepots",
            csrf_header_name="X-CSRF-Token",
            csrf_header_value="secret",
            options={"game_bundle_source": "https://example.test/main.js"},
            round_series_id=1,
        )
        bundle = (
            'bet_type:"betting" action:"spin" action:"bonus" '
            'purchased_feature:"buy_bonus" purchased_feature:"buy_chance" '
            'state_lock'
        )
        with patch(
            "tester_spin.providers.bgaming.hyperhive._download_bundle",
            return_value=bundle,
        ):
            modes = discover_modes_from_bundle(runtime, timeout_s=1)
        by_id = {mode["id"]: mode for mode in modes}
        self.assertEqual(
            by_id["SPIN"]["request"],
            {"bet_type": "betting", "action": "spin"},
        )
        self.assertEqual(
            by_id["PURCHASE_BUY_CHANCE"]["request"],
            {"purchased_feature": "buy_chance", "bet_type": "betting"},
        )
        self.assertEqual(
            by_id["PURCHASE_BUY_BONUS"]["request"],
            {"purchased_feature": "buy_bonus", "bet_type": "betting"},
        )

    def test_big_bucks_bundle_uses_bet_only_and_buy_bonus_x120(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://big-bucks-saloon.demo.bgaming-network.com/hyperhive",
            api_url="https://unused.example/api/session",
            identifier="BigBucksSaloon",
            csrf_header_name="X-CSRF-Token",
            csrf_header_value="secret",
            options={"game_bundle_source": "https://example.test/bundle.js"},
            round_series_id=1,
        )
        bundle = (
            'var a={req:{bet:s.A.data.bet}};'
            'this.isFreebets&&(a.req.bet_type="freebet");'
            'buyBonus(){this.spin(!0,{purchased_feature:"buy_bonus"})}'
            'this.buyBonusMultiplier=0;'
            'this.buyBonusMultiplier=120;'
            'jsonrpc:"2.0"'
        )
        with patch(
            "tester_spin.providers.bgaming.hyperhive._download_bundle",
            return_value=bundle,
        ):
            modes = discover_modes_from_bundle(runtime, timeout_s=1)
        by_id = {mode["id"]: mode for mode in modes}
        self.assertEqual(by_id["SPIN"]["request"], {})
        self.assertEqual(
            by_id["PURCHASE_BUY_BONUS"]["request"],
            {"purchased_feature": "buy_bonus"},
        )
        self.assertEqual(
            by_id["PURCHASE_BUY_BONUS"]["expected_multiplier"],
            120.0,
        )

    def test_big_bucks_round_win_is_used_as_cumulative_total(self) -> None:
        data = {
            "id": 0,
            "jsonrpc": "2.0",
            "result": {
                "final": True,
                "balance": 102614,
                "state_lock": "lock",
                "resp": {
                    "round": {
                        "mid": "SHOP",
                        "csid": 12,
                        "win": "7454",
                        "bet": {
                            "tb": "40",
                            "cmx": "120.000",
                        },
                    }
                },
            },
        }
        summary = _result_summary(data)
        self.assertEqual(summary["total_win"], 7454.0)
        self.assertEqual(summary["balance"], 102614)

    def test_nested_hyperhive_game_total_win_is_accepted(self) -> None:
        data = {
            "result": {
                "final": True,
                "balance": 86860,
                "state_lock": "lock-2",
                "resp": {
                    "nextAction": "SPIN",
                    "game": {"totalWin": 160},
                },
            }
        }
        summary = _result_summary(data)
        self.assertEqual(summary["total_win"], 160)
        self.assertEqual(summary["state_lock"], "lock-2")
        self.assertEqual(summary["next_action"], "SPIN")

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
