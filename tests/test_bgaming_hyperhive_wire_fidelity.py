from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.providers.bgaming.hyperhive_har import analyze_hyperhive_har
from tester_spin.providers.bgaming.hyperhive_har_bridge import apply_authoritative_har_wire
from tester_spin.providers.bgaming.hyperhive_har_script_bridge import (
    _apply_profile,
    _merge_script_modes,
    _script_rpc_id_zero,
)
from tester_spin.providers.bgaming.hyperhive_wire import analyze_engine_wire


class BGamingHyperHiveWireFidelityTests(unittest.TestCase):
    def test_full_har_replays_observed_bet_shape_and_empty_state_lock(self) -> None:
        request_payload = {
            "id": "00000000-0000-4000-8000-000000000001",
            "jsonrpc": "2.0",
            "method": "play",
            "params": {
                "token": "captured-secret",
                "req": {
                    "bet": "1.00",
                    "bet_type": "bet",
                    "custom_req": {
                        "action": "spin",
                        "exponent": 2,
                        "stake": "1.00",
                    },
                },
                "state_lock": "",
                "client_version": 7,
            },
        }
        har = {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "request": {
                            "method": "POST",
                            "url": "https://demo.bgaming-network.com/api",
                            "postData": {
                                "mimeType": "application/json",
                                "text": json.dumps(request_payload),
                            },
                        },
                        "response": {"status": 200, "content": {"text": "{}"}},
                    }
                ],
            }
        }

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "browser.har"
            path.write_text(json.dumps(har), encoding="utf-8")
            evidence = analyze_hyperhive_har(path)
            adapted = apply_authoritative_har_wire(
                {
                    "token": "fresh-token",
                    "req": {
                        "bet": 200,
                        "bet_type": "wrong",
                        "action": "spin",
                    },
                },
                evidence,
            )

        self.assertTrue(evidence.usable)
        self.assertEqual(adapted["token"], "fresh-token")
        self.assertEqual(adapted["state_lock"], "")
        self.assertEqual(adapted["client_version"], 7)
        self.assertEqual(adapted["req"]["bet"], "1.00")
        self.assertEqual(adapted["req"]["bet_type"], "bet")
        self.assertEqual(adapted["req"]["custom_req"]["stake"], "1.00")

    def test_full_har_keeps_live_state_lock_but_never_captured_lock(self) -> None:
        request_payload = {
            "id": 0,
            "jsonrpc": "2.0",
            "method": "play",
            "params": {
                "token": "captured-secret",
                "req": {"bet": 100, "bet_type": "bet", "action": "spin"},
                "state_lock": "captured-lock",
            },
        }
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "POST",
                            "url": "https://demo.bgaming-network.com/api",
                            "postData": {"text": json.dumps(request_payload)},
                        },
                        "response": {"status": 200},
                    }
                ]
            }
        }

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "browser.har"
            path.write_text(json.dumps(har), encoding="utf-8")
            evidence = analyze_hyperhive_har(path)
            adapted = apply_authoritative_har_wire(
                {
                    "token": "fresh-token",
                    "state_lock": "fresh-lock",
                    "req": {"bet": 250, "bet_type": "wrong", "action": "spin"},
                },
                evidence,
            )

        self.assertEqual(adapted["state_lock"], "fresh-lock")
        self.assertNotEqual(adapted["state_lock"], "captured-lock")
        self.assertEqual(adapted["req"]["bet"], 100)

    def test_bootstrap_har_serializer_restores_state_lock_and_bet_type(self) -> None:
        contract = (
            'a={id:MZ(),jsonrpc:"2.0",method:"play",params:{token:i.token,'
            'req:{bet:t.stake,bet_type:"bet"},state_lock:""}};'
            't.formattedRequest.params.action=t.type;'
            't.formattedRequest.params.exponent=i.getCurrencyMaxExponent();'
            'a.params.req.custom_req=t.formattedRequest.params;'
        )
        profile = analyze_engine_wire(contract)
        adapted = _apply_profile(
            {
                "token": "fresh-token",
                "req": {"bet": 100, "action": "spin"},
            },
            profile,
            contract,
        )

        self.assertEqual(adapted["state_lock"], "")
        self.assertEqual(adapted["req"]["bet_type"], "bet")
        self.assertEqual(adapted["req"]["bet"], 100)
        self.assertEqual(adapted["req"]["custom_req"]["action"], "spin")
        self.assertEqual(adapted["req"]["custom_req"]["exponent"], 2)

    def test_bootstrap_har_can_authorize_proven_base_play_contract(self) -> None:
        contract = (
            'a={id:0,jsonrpc:"2.0",method:"play",params:{token:i.token,'
            'req:{bet:t.stake,bet_type:"bet"},state_lock:""}};'
        )
        profile = analyze_engine_wire(contract)
        modes = [
            {
                "id": "SPIN",
                "kind": "SPIN",
                "request": {},
                "executable": False,
                "discovery_state": "CONTRACT_UNRESOLVED",
                "source": "live-client-evidence-incomplete",
            }
        ]
        merged = _merge_script_modes(
            modes,
            profile,
            contract,
            purchase_feature_names=lambda _text: set(),
            variant_suffix=lambda _variant, index: f"VARIANT_{index}",
        )

        self.assertTrue(merged[0]["executable"])
        self.assertEqual(merged[0]["request"]["bet_type"], "bet")
        self.assertEqual(merged[0]["discovery_state"], "HAR_SCRIPT_OBSERVED")
        self.assertEqual(merged[0]["source"], "selected-har-script-contract")
        self.assertTrue(_script_rpc_id_zero(contract))


if __name__ == "__main__":
    unittest.main()
