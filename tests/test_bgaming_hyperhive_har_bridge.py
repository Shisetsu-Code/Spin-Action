from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.providers.bgaming.hyperhive_har import analyze_hyperhive_har
from tester_spin.providers.bgaming.hyperhive_har_bridge import (
    apply_authoritative_har_wire,
    merge_har_modes,
)


class BGamingHyperHiveHARBridgeTests(unittest.TestCase):
    @staticmethod
    def _entry(req: dict, rpc_id: object) -> dict:
        return {
            "request": {
                "method": "POST",
                "url": "https://game.demo.bgaming-network.com/api",
                "postData": {
                    "mimeType": "application/json",
                    "text": json.dumps(
                        {
                            "id": rpc_id,
                            "jsonrpc": "2.0",
                            "method": "play",
                            "params": {
                                "token": "secret",
                                "req": req,
                                "state_lock": "lock",
                            },
                        }
                    ),
                },
            },
            "response": {"status": 200, "content": {"text": "{}"}},
        }

    def _evidence(self):
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "wire.har"
        entries = [
            self._entry(
                {
                    "bet": 100,
                    "bet_type": "bet",
                    "custom_req": {
                        "isNormalBuy": False,
                        "isSuperBuy": False,
                        "action": "spin",
                        "exponent": 2,
                    },
                },
                "00000000-0000-4000-8000-000000000001",
            ),
            self._entry(
                {
                    "bet": 100,
                    "bet_type": "bet",
                    "purchased_feature": "buy_bonus",
                    "custom_req": {
                        "isNormalBuy": True,
                        "isSuperBuy": False,
                        "action": "spin",
                        "exponent": 2,
                    },
                },
                "00000000-0000-4000-8000-000000000002",
            ),
            self._entry(
                {
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
                "00000000-0000-4000-8000-000000000003",
            ),
            self._entry(
                {
                    "bet": 100,
                    "bet_type": "bet",
                    "custom_req": {
                        "action": "jackpot_respin",
                        "exponent": 2,
                    },
                },
                "00000000-0000-4000-8000-000000000004",
            ),
        ]
        path.write_text(
            json.dumps({"log": {"version": "1.2", "entries": entries}}),
            encoding="utf-8",
        )
        return temp, analyze_hyperhive_har(path)

    def test_har_replaces_guessed_spin_with_observed_request(self) -> None:
        temp, evidence = self._evidence()
        try:
            adapted = apply_authoritative_har_wire(
                {
                    "token": "fresh",
                    "state_lock": "fresh-lock",
                    "req": {
                        "bet": 250,
                        "bet_type": "wrong",
                        "action": "spin",
                    },
                },
                evidence,
            )
        finally:
            temp.cleanup()

        self.assertEqual(
            adapted["req"],
            {
                "bet": 100,
                "bet_type": "bet",
                "custom_req": {
                    "isNormalBuy": False,
                    "isSuperBuy": False,
                    "action": "spin",
                    "exponent": 2,
                },
            },
        )
        self.assertEqual(adapted["state_lock"], "fresh-lock")

    def test_har_restores_two_purchase_variants(self) -> None:
        temp, evidence = self._evidence()
        try:
            modes = merge_har_modes(
                [
                    {
                        "id": "SPIN",
                        "kind": "SPIN",
                        "request": {"bet_type": "guessed"},
                        "executable": False,
                    },
                    {
                        "id": "PURCHASE_BUY_BONUS",
                        "kind": "PURCHASE",
                        "request": {
                            "purchased_feature": "buy_bonus",
                            "bet_type": "guessed",
                        },
                        "executable": True,
                    },
                ],
                evidence,
            )
        finally:
            temp.cleanup()

        by_id = {mode["id"]: mode for mode in modes}
        self.assertEqual(by_id["SPIN"]["discovery_state"], "HAR_OBSERVED")
        self.assertTrue(by_id["SPIN"]["executable"])
        self.assertIn("PURCHASE_BUY_BONUS_IS_NORMAL_BUY", by_id)
        self.assertIn("PURCHASE_BUY_BONUS_IS_SUPER_BUY", by_id)

    def test_har_continuation_drops_initial_purchase_selectors(self) -> None:
        temp, evidence = self._evidence()
        try:
            adapted = apply_authoritative_har_wire(
                {
                    "token": "fresh",
                    "state_lock": "next-lock",
                    "req": {
                        "bet": 100,
                        "bet_type": "bet",
                        "action": "jackpot_respin",
                        "is_guess": True,
                    },
                },
                evidence,
            )
        finally:
            temp.cleanup()

        self.assertEqual(
            adapted["req"],
            {
                "bet": 100,
                "bet_type": "bet",
                "custom_req": {
                    "action": "jackpot_respin",
                    "exponent": 2,
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
