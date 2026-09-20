from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from tester_spin.providers.bgaming.hyperhive import (
    _download_engine_contract,
    _hyperhive_rpc_id,
    _pz_custom_req,
    _result_summary,
    _wire_evidence_snippets,
    discover_action_vocabulary,
    discover_modes_from_bundle,
    is_hyperhive_runtime,
    run_hyperhive_test,
)
from tester_spin.models import Game
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
            'req:{bet:x,bet_type:"betting",action:"spin"} action:"bonus" '
            'purchased_feature:"buy_bonus" purchased_feature:"buy_chance" '
            'state_lock'
        )
        with patch(
            "tester_spin.providers.bgaming.hyperhive._download_bundle",
            return_value=bundle,
        ):
            modes = discover_modes_from_bundle(runtime, timeout_s=1)
        by_id = {mode["id"]: mode for mode in modes}
        self.assertTrue(by_id["SPIN"]["executable"])
        self.assertEqual(
            by_id["SPIN"]["request"],
            {"bet_type": "betting", "action": "spin"},
        )
        self.assertEqual(
            by_id["PURCHASE_BUY_CHANCE"]["request"],
            {"purchased_feature": "buy_chance", "bet_type": "betting"},
        )
        self.assertTrue(by_id["PURCHASE_BUY_CHANCE"]["executable"])
        self.assertEqual(
            by_id["PURCHASE_BUY_BONUS"]["request"],
            {"purchased_feature": "buy_bonus", "bet_type": "betting"},
        )
        self.assertTrue(by_id["PURCHASE_BUY_BONUS"]["executable"])

    def test_wire_literals_accept_assignment_and_bracket_forms(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://demo.example/hyperhive",
            api_url="https://unused.example/api/session",
            identifier="Generic",
            csrf_header_name="X-CSRF",
            csrf_header_value="secret",
            options={},
            round_series_id=1,
        )
        contract = (
            'x.req.bet=stake;'
            'x.req.bet_type="bet";'
            'x.req["action"]="spin";'
            'jsonrpc:"2.0";'
        )
        modes = discover_modes_from_bundle(
            runtime,
            timeout_s=1,
            bundle_text="",
            engine_contract=contract,
        )
        self.assertTrue(modes[0]["executable"])
        self.assertEqual(
            modes[0]["request"],
            {"bet_type": "bet", "action": "spin"},
        )
        self.assertIn(
            "spin",
            discover_action_vocabulary("", contract),
        )

    def test_engine_contract_prefers_scripts_from_launch_page(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://demo.example/hyperhive",
            api_url="https://unused.example/api/session",
            identifier="Generic",
            csrf_header_name="X-CSRF",
            csrf_header_value="secret",
            options={},
            round_series_id=1,
            script_urls=["https://cdn.example/assets/runtime-a1b2.js"],
        )

        class Response:
            text = 'jsonrpc:"2.0";x.req.bet_type="bet"'
            def raise_for_status(self) -> None:
                return None

        with patch.object(runtime.session, "get", return_value=Response()):
            contract = _download_engine_contract(runtime, timeout_s=1)

        self.assertIn('bet_type="bet"', contract)

    def test_loose_jsonrpc_literals_do_not_make_spin_executable(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://demo.example/hyperhive",
            api_url="https://unused.example/api/session",
            identifier="Generic",
            csrf_header_name="X-CSRF",
            csrf_header_value="secret",
            options={},
            round_series_id=1,
        )
        modes = discover_modes_from_bundle(
            runtime,
            timeout_s=1,
            bundle_text='action:"spin";bet_type:"bet";jsonrpc:"2.0"',
            engine_contract="",
        )
        self.assertFalse(modes[0]["executable"])
        self.assertEqual(modes[0]["discovery_state"], "CONTRACT_UNRESOLVED")
        self.assertNotIn("bet_type", modes[0]["request"])

    def test_unresolved_hyperhive_contract_is_discovery_only(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://demo.example/hyperhive",
            api_url="https://unused.example/api/session",
            identifier="Generic",
            csrf_header_name="X-CSRF",
            csrf_header_value="secret",
            options={},
            round_series_id=1,
        )
        modes = discover_modes_from_bundle(
            runtime,
            timeout_s=1,
            bundle_text="var unrelated=1;",
            engine_contract="",
        )
        self.assertFalse(modes[0]["executable"])
        self.assertEqual(
            modes[0]["discovery_state"],
            "CONTRACT_UNRESOLVED",
        )
        self.assertEqual(modes[0]["request"], {})

    def test_rpc_id_defaults_to_uuid_without_explicit_zero_contract(self) -> None:
        rpc_id = _hyperhive_rpc_id('jsonrpc:"2.0";method:"play"')
        self.assertIsInstance(rpc_id, str)
        self.assertTrue(rpc_id)

    def test_rpc_id_uses_zero_only_when_client_serializes_zero(self) -> None:
        self.assertEqual(
            _hyperhive_rpc_id('id:0,jsonrpc:"2.0",method:"play"'),
            0,
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
        self.assertTrue(by_id["SPIN"]["executable"])
        self.assertEqual(by_id["SPIN"]["request"], {})
        self.assertEqual(
            by_id["PURCHASE_BUY_BONUS"]["request"],
            {"purchased_feature": "buy_bonus"},
        )
        self.assertTrue(by_id["PURCHASE_BUY_BONUS"]["executable"])
        self.assertEqual(
            by_id["PURCHASE_BUY_BONUS"]["expected_multiplier"],
            120.0,
        )

    def test_unknown_purchase_literal_is_discovered_but_not_executable(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://demo.example/hyperhive",
            api_url="https://unused.example/api/session",
            identifier="GenericGame",
            csrf_header_name="X-CSRF-Token",
            csrf_header_value="secret",
            options={},
            round_series_id=1,
        )
        modes = discover_modes_from_bundle(
            runtime,
            timeout_s=1,
            bundle_text='purchased_feature:"future_feature"',
        )
        by_id = {mode["id"]: mode for mode in modes}
        candidate = by_id["PURCHASE_FUTURE_FEATURE"]
        self.assertFalse(candidate["executable"])
        self.assertEqual(
            candidate["discovery_state"],
            "DISCOVERED_LITERAL_ONLY",
        )

    def test_hyperhive_action_vocabulary_is_discovered_from_client_code(self) -> None:
        actions = discover_action_vocabulary(
            'action:"spin" action:"bonus"',
            "var x={action:'respin'};",
        )
        self.assertEqual(actions, {"spin", "bonus", "respin"})
        self.assertNotIn("choose_future", actions)

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

    def test_bling_blitz_discovers_custom_req_engine_profile(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://bling.example/hyperhive",
            api_url="https://bling.example/api/session",
            identifier="BlingBlitzDiamondDrop",
            csrf_header_name="X-CSRF-Token",
            csrf_header_value="secret",
            options={},
            round_series_id=1,
        )
        engine = (
            'params:{token:x,req:{bet:t.stake,bet_type:"bet"}};'
            'c.params.req.custom_req=t.formattedRequest.params;'
            'params:{selectedWinLines:[0],perLine:!0};'
        )
        modes = discover_modes_from_bundle(
            runtime,
            timeout_s=1,
            bundle_text="",
            engine_contract=engine,
        )
        self.assertTrue(modes[0]["executable"])
        self.assertEqual(modes[0]["request"], {"bet_type": "bet"})
        self.assertEqual(modes[0]["custom_req_profile"], "pz-per-line")
        self.assertEqual(
            _pz_custom_req(bet=200, exponent=2, action="spin"),
            {
                "selectedWinLines": [0],
                "perLine": True,
                "action": "spin",
                "exponent": 2,
                "stake": 200,
            },
        )

    def test_historical_minimal_hyperhive_without_wire_is_not_sent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = BGamingRuntime(
                session=requests.Session(),
                launch_url="https://blackbeards-bounty.demo.bgaming-network.com/hyperhive",
                api_url="https://unused.example/api/session",
                identifier="BlackbeardsBounty",
                csrf_header_name="X-CSRF-Token",
                csrf_header_value="secret",
                options={"play_token": "secret-token"},
                round_series_id=1,
            )
            game = Game(
                provider="bgaming",
                slug="blackbeards-bounty",
                name="Blackbeard's Bounty",
                url=runtime.launch_url,
                symbol="BlackbeardsBounty",
            )
            calls: list[tuple[str, dict, object]] = []

            class Response:
                status_code = 200

            def fake_rpc(_runtime, method, *, timeout_s, params, rpc_id=None):
                calls.append((method, params, rpc_id))
                if method == "init":
                    return (
                        Response(),
                        {"id": rpc_id, "method": "init", "params": params},
                        {
                            "result": {
                                "config": {"default_bet": 100},
                                "balance": 100000,
                            }
                        },
                    )
                self.fail("play no debe enviarse sin contrato demostrado")

            with (
                patch(
                    "tester_spin.providers.bgaming.hyperhive._download_bundle",
                    return_value="",
                ),
                patch(
                    "tester_spin.providers.bgaming.hyperhive._download_engine_contract",
                    return_value="",
                ),
                patch(
                    "tester_spin.providers.bgaming.hyperhive._rpc",
                    side_effect=fake_rpc,
                ),
            ):
                result = run_hyperhive_test(
                    game=game,
                    runtime=runtime,
                    spins=1,
                    timeout_s=1,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                    run_dir=Path(temp) / "bgaming-http-api-v2",
                    started_iso="2026-09-10T00:00:00+00:00",
                    started_monotonic=time.monotonic(),
                )

            self.assertEqual(result.status, "PARCIAL")
            self.assertEqual(result.successful_spins, 0)
            self.assertIn("CONTRACT_UNRESOLVED", result.error)
            self.assertEqual([call[0] for call in calls], ["init"])
            self.assertIsInstance(calls[0][2], str)

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


    def test_wire_evidence_snippets_are_structural_and_redacted(self) -> None:
        contract = (
            'x={id:0,jsonrpc:"2.0",method:"play",params:{token:"secret-token",'
            'req:{bet:t.stake,bet_type:"bet",action:"spin"},'
            'state_lock:"secret-lock"}};'
        )
        rows = _wire_evidence_snippets(contract, radius=80)
        kinds = {row["kind"] for row in rows}
        self.assertIn("method-play", kinds)
        self.assertIn("req-bet", kinds)
        self.assertIn("req-bet-type", kinds)
        self.assertIn("rpc-id-zero", kinds)
        joined = " ".join(row["snippet"] for row in rows)
        self.assertNotIn("secret-token", joined)
        self.assertNotIn("secret-lock", joined)
        self.assertIn("<redacted>", joined)


if __name__ == "__main__":
    unittest.main()
