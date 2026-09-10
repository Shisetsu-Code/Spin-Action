from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.providers.bgaming.har_select import inspect_har
from tester_spin.providers.bgaming.hyperhive_har import (
    analyze_hyperhive_har,
    apply_har_play_wire,
)


class BGamingHyperHiveHARVariantTests(unittest.TestCase):
    @staticmethod
    def _entry(req: dict, *, rpc_id: str) -> dict:
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
                                "state_lock": "dynamic-lock",
                            },
                        }
                    ),
                },
            },
            "response": {"status": 200, "content": {"text": "{}"}},
        }

    def _write_full_contract(self, path: Path) -> None:
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
                rpc_id="00000000-0000-4000-8000-000000000001",
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
                rpc_id="00000000-0000-4000-8000-000000000002",
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
                rpc_id="00000000-0000-4000-8000-000000000003",
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
                rpc_id="00000000-0000-4000-8000-000000000004",
            ),
            self._entry(
                {
                    "bet": 100,
                    "bet_type": "bet",
                    "custom_req": {
                        "action": "fg_jackpot_respin",
                        "exponent": 2,
                    },
                },
                rpc_id="00000000-0000-4000-8000-000000000005",
            ),
        ]
        path.write_text(
            json.dumps({"log": {"version": "1.2", "entries": entries}}),
            encoding="utf-8",
        )

    def test_same_purchase_feature_is_partitioned_by_custom_req_discriminators(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "full.har"
            self._write_full_contract(path)
            evidence = analyze_hyperhive_har(path)

        self.assertEqual(evidence.play_count, 5)
        self.assertEqual(evidence.purchase_features, {"buy_bonus"})
        self.assertEqual(evidence.purchase_variant_count, 2)
        self.assertNotIn("buy_bonus", evidence.purchases)

        variants = evidence.purchase_variants["buy_bonus"]
        selectors = {
            (
                variant.custom_req.get("isNormalBuy"),
                variant.custom_req.get("isSuperBuy"),
            )
            for variant in variants
        }
        self.assertEqual(selectors, {(True, False), (False, True)})
        self.assertTrue(
            all(variant.custom_req.get("action") == "spin" for variant in variants)
        )
        self.assertTrue(
            all(variant.custom_req.get("exponent") == 2 for variant in variants)
        )

    def test_normal_and_super_requests_select_their_exact_observed_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "full.har"
            self._write_full_contract(path)
            evidence = analyze_hyperhive_har(path)

        for normal, super_buy in ((True, False), (False, True)):
            adapted = apply_har_play_wire(
                {
                    "token": "fresh-token",
                    "state_lock": "fresh-lock",
                    "req": {
                        "bet": 250,
                        "bet_type": "bet",
                        "purchased_feature": "buy_bonus",
                        "custom_req": {
                            "isNormalBuy": normal,
                            "isSuperBuy": super_buy,
                            "action": "spin",
                            "exponent": 2,
                        },
                    },
                },
                evidence,
            )
            self.assertEqual(
                adapted["req"]["custom_req"],
                {
                    "isNormalBuy": normal,
                    "isSuperBuy": super_buy,
                    "action": "spin",
                    "exponent": 2,
                },
            )
            self.assertEqual(adapted["req"]["bet"], 250)
            self.assertEqual(adapted["state_lock"], "fresh-lock")

    def test_partial_selector_can_resolve_variant_but_feature_only_never_guesses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "full.har"
            self._write_full_contract(path)
            evidence = analyze_hyperhive_har(path)

        super_only = apply_har_play_wire(
            {
                "token": "fresh-token",
                "req": {
                    "bet": 100,
                    "purchased_feature": "buy_bonus",
                    "custom_req": {"isSuperBuy": True},
                },
            },
            evidence,
        )
        self.assertEqual(
            super_only["req"]["custom_req"],
            {
                "isNormalBuy": False,
                "isSuperBuy": True,
                "action": "spin",
                "exponent": 2,
            },
        )

        ambiguous = apply_har_play_wire(
            {
                "token": "fresh-token",
                "req": {
                    "bet": 100,
                    "purchased_feature": "buy_bonus",
                },
            },
            evidence,
        )
        self.assertNotIn("custom_req", ambiguous["req"])

    def test_continuations_remain_separate_and_do_not_inherit_buy_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "full.har"
            self._write_full_contract(path)
            evidence = analyze_hyperhive_har(path)

        self.assertEqual(
            evidence.continuations["jackpot_respin"].custom_req,
            {"action": "jackpot_respin", "exponent": 2},
        )
        self.assertEqual(
            evidence.continuations["fg_jackpot_respin"].custom_req,
            {"action": "fg_jackpot_respin", "exponent": 2},
        )

        adapted = apply_har_play_wire(
            {
                "token": "fresh-token",
                "req": {
                    "bet": 100,
                    "bet_type": "bet",
                    "custom_req": {
                        "isNormalBuy": False,
                        "isSuperBuy": True,
                        "action": "fg_jackpot_respin",
                        "exponent": 2,
                    },
                },
            },
            evidence,
        )
        self.assertEqual(
            adapted["req"]["custom_req"],
            {"action": "fg_jackpot_respin", "exponent": 2},
        )

    def test_har_quality_counts_two_purchase_modes_even_with_one_feature_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "full.har"
            self._write_full_contract(path)
            quality = inspect_har(path)

        self.assertIsNotNone(quality)
        self.assertEqual(quality.grade, "HAR_WITH_PURCHASE")
        self.assertEqual(quality.plays, 5)
        self.assertEqual(quality.purchases, 2)


if __name__ == "__main__":
    unittest.main()
