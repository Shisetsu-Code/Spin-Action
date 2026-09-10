from __future__ import annotations

import unittest

from tester_spin.providers.bgaming import hyperhive_wire
from tester_spin.providers.bgaming.hyperhive_wire import (
    ObservedHyperHiveWire,
    analyze_engine_wire,
    apply_observed_play_wire,
)


class BGamingHyperHiveWireTests(unittest.TestCase):
    def test_detects_formatted_custom_req_without_game_name_rules(self) -> None:
        contract = (
            'a={params:{req:{bet:t.stake,bet_type:"bet"}}};'
            't.formattedRequest.params.isNormalBuy=false;'
            't.formattedRequest.params.isSuperBuy=false;'
            't.formattedRequest.params.action=t.type;'
            't.formattedRequest.params.exponent=i.getCurrencyMaxExponent();'
            'a.params.req.custom_req=t.formattedRequest.params;'
        )
        profile = analyze_engine_wire(contract)
        self.assertTrue(profile.custom_req)
        self.assertTrue(profile.custom_action)
        self.assertTrue(profile.custom_exponent)
        self.assertFalse(profile.custom_stake_on_spin)
        self.assertEqual(
            profile.custom_literals,
            {"isNormalBuy": False, "isSuperBuy": False},
        )
        self.assertEqual(profile.custom_profile, "observed-formatted")

        params = {
            "token": "secret",
            "state_lock": "lock",
            "req": {"bet": 100, "bet_type": "bet"},
        }
        adapted = apply_observed_play_wire(params, profile)
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
        self.assertNotIn("custom_req", params["req"])

    def test_detects_literal_flags_inside_formatted_params_object(self) -> None:
        contract = (
            't.formattedRequest.params={isNormalBuy:false,isSuperBuy:false,foo:7};'
            't.formattedRequest.params.action=t.type;'
            't.formattedRequest.params.exponent=i.getCurrencyMaxExponent();'
            'a.params.req.custom_req=t.formattedRequest.params;'
        )
        profile = analyze_engine_wire(contract)
        self.assertEqual(
            profile.custom_literals,
            {"isNormalBuy": False, "isSuperBuy": False, "foo": 7},
        )

    def test_detects_formatted_custom_req_with_spin_stake(self) -> None:
        contract = (
            'c={params:{req:{bet:t.stake,bet_type:"bet"}}};'
            't.formattedRequest.params.action=t.type;'
            't.formattedRequest.params.exponent=i.getCurrencyMaxExponent();'
            't.type===X.SPIN&&(t.formattedRequest.params.stake=t.stake);'
            'c.params.req.custom_req=t.formattedRequest.params;'
        )
        profile = analyze_engine_wire(contract)
        profile.exponent = 3
        self.assertEqual(profile.custom_profile, "observed-formatted-stake")

        adapted = apply_observed_play_wire(
            {
                "token": "secret",
                "req": {"bet": 200, "bet_type": "bet"},
            },
            profile,
        )
        self.assertEqual(
            adapted["req"]["custom_req"],
            {"action": "spin", "exponent": 3, "stake": 200},
        )

    def test_continuation_action_moves_inside_observed_custom_req(self) -> None:
        profile = ObservedHyperHiveWire(
            custom_req=True,
            custom_action=True,
            custom_exponent=True,
            custom_stake_on_spin=True,
            exponent=2,
        )
        adapted = apply_observed_play_wire(
            {
                "token": "secret",
                "req": {
                    "bet": 200,
                    "bet_type": "bet",
                    "action": "freespin",
                },
            },
            profile,
        )
        self.assertNotIn("action", adapted["req"])
        self.assertEqual(
            adapted["req"]["custom_req"],
            {"action": "freespin", "exponent": 2},
        )

    def test_detects_generated_model_default_bet_type(self) -> None:
        contract = (
            'class R{toJson(){return{bet:this.bet,bet_type:this.betType,'
            'purchased_feature:this.purchasedFeature}}}'
            'const r=new R;r.bet=100;r.betType="default";'
        )
        profile = analyze_engine_wire(contract)
        self.assertEqual(profile.bet_type, "default")
        self.assertFalse(profile.custom_req)

        adapted = apply_observed_play_wire(
            {
                "token": "secret",
                "req": {"bet": 100, "bet_type": "bet"},
            },
            profile,
        )
        self.assertEqual(adapted["req"]["bet_type"], "default")

    def test_default_literal_without_wire_mapping_is_not_trusted(self) -> None:
        profile = analyze_engine_wire('unrelated.betType="default";')
        self.assertEqual(profile.bet_type, "")

    def test_existing_custom_req_is_never_overwritten(self) -> None:
        profile = ObservedHyperHiveWire(
            custom_req=True,
            custom_action=True,
            custom_exponent=True,
            custom_stake_on_spin=True,
            custom_literals={"isNormalBuy": False, "isSuperBuy": False},
        )
        original = {
            "selectedWinLines": [0],
            "perLine": True,
            "action": "spin",
            "exponent": 2,
            "stake": 200,
        }
        adapted = apply_observed_play_wire(
            {
                "token": "secret",
                "req": {
                    "bet": 200,
                    "bet_type": "bet",
                    "custom_req": original,
                },
            },
            profile,
        )
        self.assertEqual(adapted["req"]["custom_req"], original)

    def test_full_har_observed_godfather_contract_is_learned_from_client_code(self) -> None:
        # Sanitized structural excerpts from a full browser HAR. These are client
        # code facts, not a game-name route and contain no session credentials.
        contract = (
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
        profile = analyze_engine_wire(contract)

        self.assertTrue(profile.custom_req)
        self.assertTrue(profile.custom_action)
        self.assertTrue(profile.custom_exponent)
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
        self.assertEqual(
            hyperhive_wire._purchase_feature_names(contract),
            {"buy_bonus"},
        )
        self.assertTrue(
            {"spin", "freespin", "jackpot_respin", "fg_jackpot_respin"}
            <= hyperhive_wire._client_action_enum_values(contract)
        )

        adapted = apply_observed_play_wire(
            {
                "token": "fresh-token",
                "state_lock": "fresh-lock",
                "req": {"bet": 100, "bet_type": "bet"},
            },
            profile,
        )
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

    def test_purchase_variant_preserves_selector_and_refreshes_dynamic_exponent(self) -> None:
        profile = ObservedHyperHiveWire(
            custom_req=True,
            custom_action=True,
            custom_exponent=True,
            custom_literals={"isNormalBuy": False, "isSuperBuy": False},
            exponent=3,
        )
        adapted = apply_observed_play_wire(
            {
                "token": "fresh-token",
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
            },
            profile,
        )
        self.assertEqual(
            adapted["req"]["custom_req"],
            {
                "isNormalBuy": False,
                "isSuperBuy": True,
                "action": "spin",
                "exponent": 3,
            },
        )


if __name__ == "__main__":
    unittest.main()
