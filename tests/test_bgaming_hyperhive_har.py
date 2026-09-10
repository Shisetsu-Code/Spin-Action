from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.providers.bgaming.hyperhive_har import (
    analyze_hyperhive_har,
    apply_har_play_wire,
)
from tester_spin.providers.bgaming.hyperhive_wire import _har_result_summary


class BGamingHyperHiveHARTests(unittest.TestCase):
    def _write_har(self, path: Path) -> None:
        def entry(payload: dict) -> dict:
            return {
                "request": {
                    "method": "POST",
                    "url": "https://game.demo.bgaming-network.com/api",
                    "postData": {
                        "mimeType": "application/json",
                        "text": json.dumps(payload),
                    },
                },
                "response": {"status": 200, "content": {"text": "{}"}},
            }

        payload = {
            "log": {
                "version": "1.2",
                "entries": [
                    entry(
                        {
                            "id": "841e28dc-5a10-8dc6-f7d6-ed1c030a722d",
                            "jsonrpc": "2.0",
                            "method": "play",
                            "params": {
                                "token": "secret",
                                "req": {
                                    "bet": 100,
                                    "bet_type": "bet",
                                    "custom_req": {
                                        "isNormalBuy": False,
                                        "isSuperBuy": False,
                                        "action": "spin",
                                        "exponent": 2,
                                    },
                                },
                                "state_lock": "dynamic-lock",
                            },
                        }
                    ),
                    entry(
                        {
                            "id": "51085368-b111-4e00-8f76-8f2bcf7e35b3",
                            "jsonrpc": "2.0",
                            "method": "play",
                            "params": {
                                "token": "secret",
                                "req": {
                                    "bet": 100,
                                    "bet_type": "bet",
                                    "purchased_feature": "buy_bonus",
                                    "custom_req": {
                                        "isNormalBuy": False,
                                        "isSuperBuy": True,
                                        "action": "spin",
                                        "exponent": 2,
                                    },
                                },
                                "state_lock": "dynamic-lock-2",
                            },
                        }
                    ),
                    entry(
                        {
                            "id": "595bb23b-21ac-e780-2ce9-cad5c7123b39",
                            "jsonrpc": "2.0",
                            "method": "play",
                            "params": {
                                "token": "secret",
                                "req": {
                                    "bet": 100,
                                    "bet_type": "bet",
                                    "custom_req": {
                                        "action": "fg_jackpot_respin",
                                        "exponent": 2,
                                    },
                                },
                                "state_lock": "dynamic-lock-3",
                            },
                        }
                    ),
                ],
            }
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_extracts_exact_spin_purchase_and_continuation_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "browser.har"
            self._write_har(path)
            evidence = analyze_hyperhive_har(path)

        self.assertTrue(evidence.usable)
        self.assertEqual(evidence.play_count, 3)
        self.assertEqual(evidence.bet_type, "bet")
        self.assertEqual(evidence.rpc_id_profile, "uuid")
        self.assertEqual(
            evidence.spin.custom_req,
            {
                "isNormalBuy": False,
                "isSuperBuy": False,
                "action": "spin",
                "exponent": 2,
            },
        )
        self.assertEqual(
            evidence.purchases["buy_bonus"].custom_req,
            {
                "isNormalBuy": False,
                "isSuperBuy": True,
                "action": "spin",
                "exponent": 2,
            },
        )
        self.assertEqual(
            evidence.continuations["fg_jackpot_respin"].custom_req,
            {"action": "fg_jackpot_respin", "exponent": 2},
        )
        self.assertIn("fg_jackpot_respin", evidence.actions)

    def test_har_template_overrides_incomplete_static_custom_req(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "browser.har"
            self._write_har(path)
            evidence = analyze_hyperhive_har(path)

        adapted = apply_har_play_wire(
            {
                "token": "fresh-token",
                "req": {
                    "bet": 200,
                    "bet_type": "bet",
                    "custom_req": {"action": "spin", "exponent": 2},
                },
                "state_lock": "fresh-lock",
            },
            evidence,
        )
        self.assertEqual(
            adapted["req"]["custom_req"],
            {
                "isNormalBuy": False,
                "isSuperBuy": False,
                "action": "spin",
                "exponent": 2,
            },
        )
        self.assertEqual(adapted["req"]["bet"], 200)
        self.assertEqual(adapted["state_lock"], "fresh-lock")

    def test_purchase_template_is_selected_by_purchased_feature(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "browser.har"
            self._write_har(path)
            evidence = analyze_hyperhive_har(path)

        adapted = apply_har_play_wire(
            {
                "token": "fresh-token",
                "req": {
                    "bet": 100,
                    "bet_type": "bet",
                    "purchased_feature": "buy_bonus",
                },
            },
            evidence,
        )
        self.assertTrue(adapted["req"]["custom_req"]["isSuperBuy"])
        self.assertFalse(adapted["req"]["custom_req"]["isNormalBuy"])

    def test_continuation_template_survives_prior_static_custom_req(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "browser.har"
            self._write_har(path)
            evidence = analyze_hyperhive_har(path)

        adapted = apply_har_play_wire(
            {
                "token": "fresh-token",
                "req": {
                    "bet": 100,
                    "bet_type": "bet",
                    "custom_req": {
                        "action": "fg_jackpot_respin",
                        "exponent": 2,
                        "stake": 100,
                    },
                },
                "state_lock": "fresh-lock",
            },
            evidence,
        )
        self.assertEqual(
            adapted["req"]["custom_req"],
            {"action": "fg_jackpot_respin", "exponent": 2},
        )
        self.assertNotIn("stake", adapted["req"]["custom_req"])

    def test_nested_engine_result_supplies_next_action_and_total_win(self) -> None:
        summary = _har_result_summary(
            {
                "result": {
                    "final": False,
                    "balance": 79800,
                    "resp": {
                        "win": 0,
                        "engine": {
                            "gamestate": {
                                "stake": 100,
                                "totalWinnings": 18980,
                                "triggeringDetails": {
                                    "nextAction": "fg_jackpot_respin"
                                },
                            }
                        },
                    },
                }
            },
            {
                "bet": None,
                "total_win": None,
                "next_action": None,
            },
        )
        self.assertEqual(summary["bet"], 100)
        self.assertEqual(summary["total_win"], 18980)
        self.assertEqual(summary["next_action"], "fg_jackpot_respin")


if __name__ == "__main__":
    unittest.main()
