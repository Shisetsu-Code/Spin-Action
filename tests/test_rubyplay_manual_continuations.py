from __future__ import annotations

import unittest

from tester_spin.providers.rubyplay.execution import SAFE_CONTINUATIONS, post_action
from tester_spin.providers.rubyplay.runtime import (
    LauncherConfig,
    RubyPlayClientProfile,
    RubyPlayRuntime,
    discover_client_profile,
)


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _ContinuationSession:
    def __init__(self, feature_type: str):
        self.feature_type = feature_type
        self.posts = []

    def post(self, url, *, json, timeout):
        self.posts.append((url, dict(json), timeout))
        return _FakeResponse(
            {
                "status": "ok",
                "topic": f"gameserver/{json['action']}",
                "data": {
                    "next_action": "spin",
                    "an": int(json["an"]) + 1,
                    "player": {"balance": 990000},
                    "buy_feature_type": self.feature_type,
                },
                "funModeData": {
                    "brandId": 427,
                    "gameId": 216,
                    "currencyId": 978,
                    "currentBalanceSubUnit": 990000,
                    "sessionData": "opaque",
                },
            }
        )


def _runtime_for(feature_type: str) -> tuple[RubyPlayRuntime, _ContinuationSession]:
    session = _ContinuationSession(feature_type)
    runtime = RubyPlayRuntime(
        session=session,  # type: ignore[arg-type]
        launcher=LauncherConfig(
            launcher_url=(
                "https://prrpeu3.com/launcher?gamename=rp_test&operator=rubyplay.com"
                "&server_url=https://srv.prrpeu3.com&currency=EUR&mode=fun&lang=en"
            ),
            gamename="rp_test",
            operator="rubyplay.com",
            server_url="https://srv.prrpeu3.com",
            currency="EUR",
            mode="fun",
            lang="en",
        ),
        client_profile=RubyPlayClientProfile(
            protocol_version=2,
            math_version=2025103096,
            actions=["init", "spin", "freespin", "respin", "buy_feature"],
        ),
        session_key="secret",
        fun_mode_data={"gameId": 216, "sessionData": "opaque"},
        init_data={},
        bets=[10, 100],
        default_bet=100,
        default_bet_index=1,
        currency="EUR",
        subunit=100,
        action_number=3,
        next_action=feature_type,
        active_feature_type=feature_type,
    )
    return runtime, session


class RubyPlayManualContinuationTests(unittest.TestCase):
    def test_scientific_notation_math_version_is_not_truncated(self) -> None:
        bundle = (
            'tt.VERSION=2;tt.__class="com.gongxigames.math.core.binary.BinarySerializer";'
            'var z=class{static MATH_VERSION_$LI$(){return z.MATH_VERSION==null&&'
            '(z.MATH_VERSION=2025103e3+z.RTP),z.MATH_VERSION}};'
            'z.RTP=96,z.WAGER=1;'
        )
        profile = discover_client_profile([("https://cdn.example/game.js", bundle)])
        self.assertEqual(profile.protocol_version, 2)
        self.assertEqual(profile.rtp, 96.0)
        self.assertEqual(profile.math_version, 2025103096)

    def test_active_slot_engine_wins_over_stale_embedded_math_table(self) -> None:
        bundle = (
            'tt.VERSION=2;tt.__class="com.gongxigames.math.core.binary.BinarySerializer";'
            'I.MATH_VERSION=2024071596,I.RTP=96,I.WAGER=10;'
            'pt.MATH_VERSION=2023081496,pt.RTP=96,pt.WAGER=50;'
            'var Zd=class extends hy{constructor(){super(I.MATH_VERSION),this.x=1}'
            'getMaxWager(){return I.WAGER}};'
            'Zd.__class="com.gongxigames.math.diamondexplosion.engine.DiamondExplosionEngine";'
            'vd.initBinaryFactory(new Zd);'
            'var D1=class{static createProxy(){return new ty(new Zd)}};'
        )
        profile = discover_client_profile([("https://cdn.example/game.js", bundle)])
        self.assertEqual(profile.protocol_version, 2)
        self.assertEqual(profile.rtp, 96.0)
        self.assertEqual(profile.math_version, 2024071596)
        self.assertEqual(profile.wager, 10.0)
        self.assertTrue(
            any("active-slot-engine.Zd->I.MATH_VERSION" in item for item in profile.evidence)
        )
        self.assertTrue(
            any("active-slot-engine.Zd->I.WAGER" in item for item in profile.evidence)
        )

    def test_freespin_manual_purchase_click_uses_proven_wire_shape(self) -> None:
        runtime, session = _runtime_for("freespin")
        _response, request, _data, previous_an = post_action(
            runtime,
            "freespin",
            timeout_s=5.0,
        )
        self.assertEqual(previous_an, 3)
        self.assertEqual(request["action"], "freespin")
        self.assertEqual(request["buy_feature_type"], "freespin")
        self.assertNotIn("bet", request)
        self.assertNotIn("buy_feature_price", request)
        self.assertEqual(runtime.action_number, 4)
        self.assertEqual(runtime.next_action, "spin")
        self.assertEqual(len(session.posts), 1)

    def test_respin_manual_purchase_click_uses_same_family_envelope(self) -> None:
        runtime, _session = _runtime_for("respin")
        _response, request, _data, _previous_an = post_action(
            runtime,
            "respin",
            timeout_s=5.0,
        )
        self.assertEqual(request["buy_feature_type"], "respin")
        self.assertNotIn("bet", request)
        self.assertNotIn("buy_feature_price", request)

    def test_only_har_proven_manual_continuations_are_auto_followed(self) -> None:
        self.assertIn("freespin", SAFE_CONTINUATIONS)
        self.assertIn("respin", SAFE_CONTINUATIONS)
        self.assertNotIn("select", SAFE_CONTINUATIONS)
        self.assertNotIn("pick", SAFE_CONTINUATIONS)


if __name__ == "__main__":
    unittest.main()
