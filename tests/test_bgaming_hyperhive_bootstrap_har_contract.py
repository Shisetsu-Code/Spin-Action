from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.providers.bgaming.hyperhive_har import clear_thread_har_path, set_thread_har_path
from tester_spin.providers.bgaming.hyperhive_har_script_bridge import current_har_script_contract
from tester_spin.providers.bgaming.hyperhive_wire import analyze_engine_wire, apply_observed_play_wire


class BGamingHyperHiveBootstrapHARContractTests(unittest.TestCase):
    def test_bootstrap_only_har_can_recover_split_serializer_contract(self) -> None:
        client = (
            'a={id:MZ(),jsonrpc:"2.0",method:"play",params:{token:i.token,'
            'req:{bet:t.stake,bet_type:"bet"},state_lock:""}};'
            't.formattedRequest.params.action=t.type;'
            't.formattedRequest.params.exponent=i.getCurrencyMaxExponent();'
            'a.params.req.custom_req=t.formattedRequest.params;'
        )
        feature = (
            'customizeFeatureBuyRequestData(t,e){'
            't.AdditionalData.params.isNormalBuy=!1;'
            't.AdditionalData.params.isSuperBuy=!1;'
            'switch(e.mode){case yS.NormalBuyBonus:'
            't.AdditionalData.params.isNormalBuy=!0;break;'
            'case yS.SuperBuyBonus:'
            't.AdditionalData.params.isSuperBuy=!0;break;}'
            'return t}setRequestConfig(t){return t}'
        )
        har = {
            "log": {
                "entries": [
                    {
                        "request": {"url": "https://demo.bgaming-network.com/client.min.js?key=aaa"},
                        "response": {"content": {"text": client}},
                    },
                    {
                        "request": {"url": "https://demo.bgaming-network.com/game/integration.min.js?key=bbb"},
                        "response": {"content": {"text": feature}},
                    },
                ]
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "browser.har"
            path.write_text(json.dumps(har), encoding="utf-8")
            set_thread_har_path(path)
            try:
                contract = current_har_script_contract()
            finally:
                clear_thread_har_path()

        self.assertIn("jsonrpc", contract)
        self.assertIn("isNormalBuy", contract)
        self.assertIn("isSuperBuy", contract)

        profile = analyze_engine_wire(contract)
        self.assertEqual(
            profile.custom_literals,
            {"isNormalBuy": False, "isSuperBuy": False},
        )
        self.assertEqual(len(profile.purchase_custom_variants), 2)

        adapted = apply_observed_play_wire(
            {
                "token": "fresh-token",
                "state_lock": "fresh-lock",
                "req": {"bet": 100, "bet_type": "bet", "action": "spin"},
            },
            profile,
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


if __name__ == "__main__":
    unittest.main()
