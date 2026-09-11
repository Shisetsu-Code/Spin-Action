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
    def __init__(self, feature_type: str = "", next_actions: list[str] | None = None):
        self.feature_type = feature_type
        self.next_actions = list(next_actions or ["spin"])
        self.posts = []

    def post(self, url, *, json, timeout):
        self.posts.append((url, dict(json), timeout))
        next_action = self.next_actions.pop(0) if self.next_actions else "spin"
        body = {
            "next_action": next_action,
            "an": int(json["an"]) + 1,
            "player": {"balance": 990000},
        }
        if self.feature_type:
            body["buy_feature_type"] = self.feature_type
        return _FakeResponse(
            {
                "status": "ok",
                "topic": f"gameserver/{json['action']}",
                "data": body,
                "funModeData": {
                    "brandId": 427,
                    "gameId": 216,
                    "currencyId": 978,
                    "currentBalanceSubUnit": 990000,
                    "sessionData": "opaque",
                },
            }
        )


def _runtime_for(
    next_action: str,
    *,
    feature_type: str | None = None,
    response_next_actions: list[str] | None = None,
) -> tuple[RubyPlayRuntime, _ContinuationSession]:
    active = next_action if feature_type is None else feature_type
    session = _ContinuationSession(active, response_next_actions)
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
            math_version=2025100696,
            actions=[
                "init",
                "spin",
                "freespin",
                "respin",
                "select",
                "pick",
                "minispin",
                "buy_feature",
            ],
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
        next_action=next_action,
        active_feature_type=active,
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

    def test_lazy_math_getter_alias_resolves_active_coffee_style_engine(self) -> None:
        bundle = (
            'tt.VERSION=2;tt.__class="com.gongxigames.math.core.binary.BinarySerializer";'
            'var f=class{static MATH_VERSION_$LI$(){return f.MATH_VERSION==null&&'
            '(f.MATH_VERSION=2025100600+f.RTP),f.MATH_VERSION}};'
            'f.RTP=96;var I=f;I.WAGER=10;'
            'pt.MATH_VERSION=2023081496,pt.RTP=96,pt.WAGER=50;'
            'var Zd=class extends hy{constructor(){super(I.MATH_VERSION_$LI$()),this.x=1}'
            'getMaxWager(){return I.WAGER}};'
            'Zd.__class="com.gongxigames.math.diamondexplosion.engine.DiamondExplosionEngine";'
            'vd.initBinaryFactory(new Zd);'
            'var D1=class{static createProxy(){return new ty(new Zd)}};'
        )
        profile = discover_client_profile([("https://cdn.example/game.js", bundle)])
        self.assertEqual(profile.protocol_version, 2)
        self.assertEqual(profile.rtp, 96.0)
        self.assertEqual(profile.math_version, 2025100696)
        self.assertEqual(profile.wager, 10.0)

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

    def test_select_uses_first_valid_index_and_keeps_purchase_type(self) -> None:
        runtime, _session = _runtime_for("select")
        _response, request, _data, _previous_an = post_action(
            runtime,
            "select",
            timeout_s=5.0,
        )
        self.assertEqual(request["action"], "select")
        self.assertEqual(request["index"], 0)
        self.assertEqual(request["buy_feature_type"], "select")
        self.assertNotIn("bet", request)
        self.assertNotIn("buy_feature_price", request)

    def test_natural_pick_chain_uses_distinct_indices(self) -> None:
        runtime, session = _runtime_for(
            "pick",
            feature_type="",
            response_next_actions=["pick", "spin"],
        )
        _response, first, _data, _previous_an = post_action(
            runtime,
            "pick",
            timeout_s=5.0,
        )
        _response, second, _data, _previous_an = post_action(
            runtime,
            "pick",
            timeout_s=5.0,
        )
        self.assertEqual(first["index"], 0)
        self.assertEqual(second["index"], 1)
        self.assertNotIn("buy_feature_type", first)
        self.assertNotIn("buy_feature_type", second)
        self.assertEqual(len(session.posts), 2)

    def test_minispin_has_no_action_specific_index(self) -> None:
        runtime, _session = _runtime_for("minispin")
        _response, request, _data, _previous_an = post_action(
            runtime,
            "minispin",
            timeout_s=5.0,
        )
        self.assertEqual(request["action"], "minispin")
        self.assertNotIn("index", request)
        self.assertEqual(request["buy_feature_type"], "minispin")

    def test_client_proven_continuations_are_auto_followed(self) -> None:
        for action in ("freespin", "respin", "select", "pick", "minispin"):
            self.assertIn(action, SAFE_CONTINUATIONS)
        self.assertNotIn("unknown", SAFE_CONTINUATIONS)


if __name__ == "__main__":
    unittest.main()
