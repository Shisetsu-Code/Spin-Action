from __future__ import annotations

import unittest

from tester_spin.providers.rubyplay.runtime import discover_client_profile


def _common_prefix(*, math_version: int, wager: int) -> str:
    return (
        'tt.VERSION=2;'
        'tt.__class="com.gongxigames.math.core.binary.BinarySerializer";'
        f'z.MATH_VERSION={math_version},z.RTP=96,z.WAGER={wager};'
        'new A(0,"x","init",0);'
        'new A(1,"x","spin",0);'
        'new A(2,"x","freespin",0);'
        'new A(3,"x","respin",0);'
        'new A(4,"x","select",0);'
        'new A(5,"x","pick",0);'
        'new A(6,"x","minispin",0);'
        'new A(10,"x","buy_feature",0);'
    )


class RubyPlayBuyFeatureDiscoveryTests(unittest.TestCase):
    def test_rush_fever_style_type_is_not_limited_by_method_distance(self) -> None:
        bundle = (
            _common_prefix(math_version=2024020796, wager=10)
            + 'getBuyFeatureType(e){return e.getAction()===10?xt._$wrappers[2].getName():'
            'this.isBuyFeatureSession()?xt._$wrappers[2].getName():"none"}'
            + ('x=0;' * 1000)
            + 'isBuyFeatureGame(){return!0}'
            + 'Ba.BUY_FEATURE_FREESPIN_COST_IN_TIMES_BET=100;'
        )
        profile = discover_client_profile([("https://cdn.example/game.js", bundle)])
        self.assertEqual(profile.buy_feature_type, "freespin")
        self.assertEqual(profile.buy_feature_multiplier, 100.0)
        self.assertIs(profile.buy_feature_client_supported, True)
        self.assertTrue(
            any("method-local" in item for item in profile.evidence),
            profile.evidence,
        )

    def test_jmania_style_duplicate_same_multiplier_remains_unambiguous(self) -> None:
        bundle = (
            _common_prefix(math_version=2025031196, wager=10)
            + 'getBuyFeatureType(e){return e.getAction()===10?St._$wrappers[2].getName():'
            'this.isBuyFeatureSession()?St._$wrappers[2].getName():"none"}'
            + ('q=1;' * 50000)
            + 'isBuyFeatureGame(){return!0}'
            + 'Aa.BUY_FEATURE_FREESPIN_COST_IN_TIMES_BET=50;'
            + 'Bb.BUY_FEATURE_FREESPIN_COST_IN_TIMES_BET=50;'
        )
        profile = discover_client_profile([("https://cdn.example/game.js", bundle)])
        self.assertEqual(profile.buy_feature_type, "freespin")
        self.assertEqual(profile.buy_feature_multiplier, 50.0)
        self.assertIs(profile.buy_feature_client_supported, True)

    def test_jmania_three_loose_cannons_composite_freespin_respin_type(self) -> None:
        bundle = (
            _common_prefix(math_version=2026032096, wager=5)
            + 'getBuyFeatureType(e){return e.getAction()===10?'
            'mt._$wrappers[2].getName()+"_"+mt._$wrappers[3].getName():'
            'this.isBuyFeatureSession()?mt._$wrappers[2].getName()+"_"+'
            'mt._$wrappers[3].getName():"none"}'
            + 'getBuyFeatureMultiplier(e){return er.BUY_FEATURE_FREESPIN_COST_IN_TIMES_BET}'
            + 'isBuyFeatureGame(){return!0}'
            + 'er.BUY_FEATURE_FREESPIN_COST_IN_TIMES_BET=100;'
        )
        profile = discover_client_profile([("https://cdn.example/game.js", bundle)])
        self.assertEqual(profile.buy_feature_type, "freespin_respin")
        self.assertEqual(profile.buy_feature_multiplier, 100.0)
        self.assertEqual(profile.wager, 5.0)
        self.assertIs(profile.buy_feature_client_supported, True)

    def test_unjoined_different_wrapper_types_remain_ambiguous(self) -> None:
        bundle = (
            _common_prefix(math_version=2026032096, wager=5)
            + 'getBuyFeatureType(e){return e.flag?mt._$wrappers[2].getName():'
            'mt._$wrappers[3].getName()}'
            + 'isBuyFeatureGame(){return!0}'
            + 'er.BUY_FEATURE_FREESPIN_COST_IN_TIMES_BET=100;'
        )
        profile = discover_client_profile([("https://cdn.example/game.js", bundle)])
        self.assertEqual(profile.buy_feature_type, "")
        self.assertIs(profile.buy_feature_client_supported, True)

    def test_legacy_optional_wrapper_plus_false_base_stays_unsupported(self) -> None:
        bundle = (
            _common_prefix(math_version=20220126, wager=10)
            + 'isBuyFeatureGame(){return!!this.getSession().isBuyFeatureGame?.()}'
            + 'isBuyFeatureGame(){return!1}'
        )
        profile = discover_client_profile([("https://cdn.example/legacy.js", bundle)])
        self.assertEqual(profile.buy_feature_type, "")
        self.assertIs(profile.buy_feature_client_supported, False)


if __name__ == "__main__":
    unittest.main()
