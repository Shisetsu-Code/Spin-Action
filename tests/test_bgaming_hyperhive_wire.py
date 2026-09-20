from __future__ import annotations

import unittest

from tester_spin.providers.bgaming import hyperhive_wire
from tester_spin.providers.bgaming.hyperhive_wire import (
    ObservedHyperHiveWire,
    _apply_observed_custom_profile_to_base_mode,
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

        continuation = apply_observed_play_wire(
            {
                "token": "fresh-token",
                "state_lock": "next-lock",
                "req": {
                    "bet": 100,
                    "bet_type": "bet",
                    "action": "jackpot_respin",
                },
            },
            profile,
        )
        self.assertEqual(
            continuation["req"]["custom_req"],
            {"action": "jackpot_respin", "exponent": 2},
        )
        self.assertNotIn("isNormalBuy", continuation["req"]["custom_req"])
        self.assertNotIn("isSuperBuy", continuation["req"]["custom_req"])

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

    def test_observed_serializer_does_not_replace_legacy_pz_profile(self) -> None:
        mode = {
            "id": "SPIN",
            "kind": "SPIN",
            "executable": True,
            "custom_req_profile": "pz-per-line",
            "discovery_state": "BASE_CONTRACT",
            "source": "engine-heuristic",
        }
        profile = ObservedHyperHiveWire(
            custom_req=True,
            custom_action=True,
            custom_exponent=True,
            custom_stake_on_spin=True,
            custom_literals={"isNormalBuy": False},
        )
        _apply_observed_custom_profile_to_base_mode(mode, profile)
        self.assertEqual(mode["custom_req_profile"], "pz-per-line")
        self.assertNotIn("custom_req_literal_keys", mode)
        self.assertEqual(mode["discovery_state"], "BASE_CONTRACT")
        self.assertEqual(mode["source"], "engine-heuristic")

    def test_no_observed_serializer_keeps_legacy_profile(self) -> None:
        mode = {"custom_req_profile": "pz-per-line", "executable": True}
        _apply_observed_custom_profile_to_base_mode(
            mode,
            ObservedHyperHiveWire(custom_req=False),
        )
        self.assertEqual(mode["custom_req_profile"], "pz-per-line")


    def test_dynamic_default_bet_type_and_req_defaults_are_applied(self) -> None:
        contract = (
            'let s=this.freeBets.isActive()?"freebet":"default";'
            'this.network.invoke("play",{token:this.network.token,'
            'req:{bet:e,bet_type:s,custom_field:"custom_value",'
            'fe_exponent:this.globalState.feBetExponent}});'
        )
        profile = analyze_engine_wire(contract)
        profile.exponent = 3
        self.assertEqual(profile.bet_type, "default")
        self.assertEqual(profile.req_literals, {"custom_field": "custom_value"})
        self.assertEqual(profile.req_exponent_fields, ["fe_exponent"])
        adapted = apply_observed_play_wire(
            {"token": "secret", "req": {"bet": 20, "bet_type": "bet"}},
            profile,
        )
        self.assertEqual(
            adapted["req"],
            {
                "bet": 20,
                "bet_type": "default",
                "custom_field": "custom_value",
                "fe_exponent": 3,
            },
        )

    def test_req_ternary_scalar_fallback_is_preserved(self) -> None:
        contract = (
            'network.invoke("play",{req:{bet:e.bet,'
            'machineId:e.spinParams?.machineId?parseInt(e.spinParams.machineId,10):0,'
            'bet_type:e.free?"freebet":void 0}});'
        )
        profile = analyze_engine_wire(contract)
        self.assertEqual(profile.req_literals.get("machineId"), 0)


    def test_req_alias_defaults_and_runtime_balance_are_applied(self) -> None:
        contract = (
            'let e=this.freeBets.isActive()?"buy_chance":null,n="default",r=1;'
            '"buy_bonus"==e&&(r=this.globalState.buyBonusModeMultiplier);'
            'this.network.invoke("play",{req:{bet:i,bet_type:n,'
            'purchased_feature:e,balance:this.globalState.balance,'
            'buyBonusModeMultiplier:r}});'
        )
        profile = analyze_engine_wire(contract)
        profile.runtime_balance = 100000
        self.assertIsNone(profile.req_alias_literals["purchased_feature"])
        self.assertEqual(profile.req_alias_literals["buyBonusModeMultiplier"], 1)
        self.assertEqual(profile.req_balance_fields, ["balance"])
        adapted = apply_observed_play_wire(
            {"req": {"bet": 10, "bet_type": "default"}},
            profile,
        )
        self.assertEqual(adapted["req"]["purchased_feature"], None)
        self.assertEqual(adapted["req"]["balance"], 100000)
        self.assertEqual(adapted["req"]["buyBonusModeMultiplier"], 1)

    def test_freebet_or_undefined_omits_normal_bet_type(self) -> None:
        contract = (
            'network.post({req:{bet:e.bet,'
            'machineId:e.spinParams?.machineId?parseInt(e.spinParams.machineId,10):0,'
            'bet_type:n.hasActiveFreeRound()?"freebet":void 0}});'
        )
        profile = analyze_engine_wire(contract)
        self.assertTrue(profile.omit_normal_bet_type)
        adapted = apply_observed_play_wire(
            {"req": {"bet": 25, "bet_type": "bet"}},
            profile,
        )
        self.assertNotIn("bet_type", adapted["req"])
        self.assertEqual(adapted["req"]["machineId"], 0)


if __name__ == "__main__":
    unittest.main()
