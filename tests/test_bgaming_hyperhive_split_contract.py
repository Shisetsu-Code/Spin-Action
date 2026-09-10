from __future__ import annotations

import unittest
from unittest.mock import Mock

import requests

from tester_spin.providers.bgaming.hyperhive_transport import (
    _append_engine_role_contracts,
)
from tester_spin.providers.bgaming.hyperhive_wire import (
    analyze_engine_wire,
    apply_observed_play_wire,
)
from tester_spin.providers.bgaming.runtime import BGamingRuntime


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class BGamingHyperHiveSplitContractTests(unittest.TestCase):
    def test_feature_selectors_survive_when_serializer_is_split_across_scripts(self) -> None:
        session = requests.Session()
        runtime = BGamingRuntime(
            session=session,
            launch_url=(
                "https://example.demo.bgaming-network.com/"
                "hyperhive?launch_token=outer"
            ),
            api_url="https://unused.example/api/session",
            identifier="Synthetic",
            csrf_header_name="X-CSRF-Token",
            csrf_header_value="csrf",
            options={"play_token": "play-token"},
            round_series_id=1,
            script_urls=[
                "https://example.demo.bgaming-network.com/client.min.js?key=aaa",
                "https://example.demo.bgaming-network.com/game/integration.min.js?key=bbb",
            ],
        )

        transport_script = (
            'a={id:MZ(),jsonrpc:"2.0",method:"play",params:{token:i.token,'
            'req:{bet:t.stake,bet_type:"bet"},state_lock:""}};'
            't.formattedRequest.params.action=t.type;'
            't.formattedRequest.params.exponent=i.getCurrencyMaxExponent();'
            'a.params.req.custom_req=t.formattedRequest.params;'
        )
        feature_script = (
            'customizeFeatureBuyRequestData(t,e){'
            't.AdditionalData.params.isNormalBuy=!1;'
            't.AdditionalData.params.isSuperBuy=!1;'
            'switch(e.mode){case yS.NormalBuyBonus:'
            't.AdditionalData.params.isNormalBuy=!0;break;'
            'case yS.SuperBuyBonus:'
            't.AdditionalData.params.isSuperBuy=!0;break;}'
            'return t}setRequestConfig(t){return t}'
        )

        def fake_get(url, *args, **kwargs):
            if "integration.min.js" in url:
                return _Response(feature_script)
            return _Response(transport_script)

        runtime.session.get = Mock(side_effect=fake_get)
        try:
            combined = _append_engine_role_contracts(
                runtime,
                transport_script,
                timeout_s=1,
            )
            profile = analyze_engine_wire(combined)
        finally:
            session.close()

        self.assertEqual(
            profile.custom_literals,
            {"isNormalBuy": False, "isSuperBuy": False},
        )
        self.assertEqual(
            profile.purchase_custom_variants,
            [
                {"isNormalBuy": True, "isSuperBuy": False},
                {"isNormalBuy": False, "isSuperBuy": True},
            ],
        )

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
