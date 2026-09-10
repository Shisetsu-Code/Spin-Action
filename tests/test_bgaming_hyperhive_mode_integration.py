from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

# Importing the active provider installs the production HyperHive adapters.
from tester_spin.providers.bgaming import BGamingProvider  # noqa: F401
from tester_spin.providers.bgaming import hyperhive
from tester_spin.providers.bgaming.runtime import BGamingRuntime


GODFATHER_LIVE_CONTRACT = (
    'a={id:MZ(),jsonrpc:"2.0",method:"play",params:{token:i.token,'
    'req:{bet:t.stake,bet_type:"bet"},state_lock:""}};'
    'true===t.buyFeature&&(a.params.req.purchased_feature=M8.buy_bonus);'
    't.formattedRequest.params.action=t.type;'
    't.formattedRequest.params.exponent=i.getCurrencyMaxExponent();'
    'a.params.req.custom_req=t.formattedRequest.params;'
    'customizeFeatureBuyRequestData(t,e){'
    't.AdditionalData.params.isNormalBuy=!1;'
    't.AdditionalData.params.isSuperBuy=!1;'
    'switch(e.mode){case yS.NormalBuyBonus:'
    't.AdditionalData.params.isNormalBuy=!0;break;'
    'case yS.SuperBuyBonus:t.AdditionalData.params.isSuperBuy=!0;break;}'
    'return t}setRequestConfig(t){return t}'
    '(Um=Nm||(Nm={})).SPIN="spin",Um.FREESPIN="freespin",'
    'Um.JACKPOT_RESPIN="jackpot_respin",'
    'Um.FREESPIN_JACKPOT_RESPIN="fg_jackpot_respin";'
)


class BGamingHyperHiveModeIntegrationTests(unittest.TestCase):
    def _runtime(self) -> BGamingRuntime:
        # A non-container launch keeps this unit test completely offline while
        # exercising the already-installed production mode-discovery wrapper.
        return BGamingRuntime(
            session=requests.Session(),
            launch_url="https://game.demo.bgaming-network.com/games/Test/FUN",
            api_url="https://unused.example/api/session",
            identifier="SyntheticContract",
            csrf_header_name="X-CSRF-Token",
            csrf_header_value="csrf",
            options={"play_token": "fresh-token"},
            round_series_id=1,
        )

    def test_full_contract_synthesizes_base_normal_and_super_modes(self) -> None:
        runtime = self._runtime()
        try:
            with patch.object(
                hyperhive,
                "_download_bundle",
                return_value=GODFATHER_LIVE_CONTRACT,
            ):
                modes = hyperhive.discover_modes_from_bundle(
                    runtime,
                    timeout_s=1,
                    bundle_text=GODFATHER_LIVE_CONTRACT,
                    engine_contract=GODFATHER_LIVE_CONTRACT,
                )
        finally:
            runtime.session.close()

        by_id = {str(mode["id"]): mode for mode in modes}
        self.assertEqual(
            set(by_id),
            {
                "SPIN",
                "PURCHASE_BUY_BONUS_IS_NORMAL_BUY",
                "PURCHASE_BUY_BONUS_IS_SUPER_BUY",
            },
        )
        self.assertTrue(all(bool(mode.get("executable")) for mode in modes))
        self.assertEqual(
            by_id["SPIN"]["custom_req_profile"],
            "observed-formatted",
        )

        normal = by_id["PURCHASE_BUY_BONUS_IS_NORMAL_BUY"]["request"]
        self.assertEqual(normal["purchased_feature"], "buy_bonus")
        self.assertEqual(normal["bet_type"], "bet")
        self.assertEqual(
            normal["custom_req"],
            {
                "isNormalBuy": True,
                "isSuperBuy": False,
                "action": "spin",
                "exponent": 2,
            },
        )

        super_buy = by_id["PURCHASE_BUY_BONUS_IS_SUPER_BUY"]["request"]
        self.assertEqual(super_buy["purchased_feature"], "buy_bonus")
        self.assertEqual(super_buy["bet_type"], "bet")
        self.assertEqual(
            super_buy["custom_req"],
            {
                "isNormalBuy": False,
                "isSuperBuy": True,
                "action": "spin",
                "exponent": 2,
            },
        )

    def test_continuation_vocabulary_includes_both_jackpot_paths(self) -> None:
        actions = hyperhive.discover_action_vocabulary(
            GODFATHER_LIVE_CONTRACT,
            GODFATHER_LIVE_CONTRACT,
        )
        self.assertIn("jackpot_respin", actions)
        self.assertIn("fg_jackpot_respin", actions)


if __name__ == "__main__":
    unittest.main()
