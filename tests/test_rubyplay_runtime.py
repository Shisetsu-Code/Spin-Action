from __future__ import annotations

import unittest

from tester_spin.providers.rubyplay.client_capability import _apply_effective_buy_capability
from tester_spin.providers.rubyplay.runtime import (
    LauncherConfig,
    RubyPlayClientProfile,
    RubyPlayRuntime,
    bet_plan_from_init,
    discover_client_profile,
    extract_launcher_url,
    parse_launcher_url,
    post_action,
    purchase_price,
    validate_action_response,
)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.posts = []

    def post(self, url, *, json, timeout):
        self.posts.append((url, dict(json), timeout))
        action = json["action"]
        return _FakeResponse(
            {
                "status": "ok",
                "topic": f"gameserver/{action}",
                "data": {
                    "next_action": "spin",
                    "an": json["an"] + 1,
                    "player": {"balance": 999900},
                },
                "funModeData": {
                    "brandId": 427,
                    "gameId": 235,
                    "currencyId": 978,
                    "currentBalanceSubUnit": 999900,
                    "sessionData": "opaque",
                },
            }
        )


class RubyPlayRuntimeTests(unittest.TestCase):
    def test_launcher_is_discovered_structurally(self) -> None:
        url = (
            "https://prrpeu3.com/launcher?gamename=rp_235&operator=rubyplay.com"
            "&server_url=https://srv.prrpeu3.com&currency=EUR&mode=fun&lang=en"
        )
        escaped = url.replace("&", "&amp;")
        html = f'<html><body><iframe src="{escaped}"></iframe></body></html>'
        self.assertEqual(
            extract_launcher_url(html, "https://rubyplay.com/games/x/"),
            url,
        )
        parsed = parse_launcher_url(url)
        self.assertEqual(parsed.gamename, "rp_235")
        self.assertEqual(parsed.server_url, "https://srv.prrpeu3.com")
        self.assertEqual(
            parsed.gameserver_url,
            "https://srv.prrpeu3.com/gameserver/demo",
        )

    def test_launcher_accepts_repeated_identical_parameters(self) -> None:
        url = (
            "https://prrpeu3.com/launcher?gamename=rp_235&operator=rubyplay.com"
            "&server_url=https://srv.prrpeu3.com&currency=EUR&currency=EUR"
            "&mode=fun&lang=en"
        )
        parsed = parse_launcher_url(url)
        self.assertEqual(parsed.currency, "EUR")
        self.assertEqual(parsed.gamename, "rp_235")
        self.assertEqual(parsed.server_url, "https://srv.prrpeu3.com")

    def test_launcher_rejects_repeated_conflicting_parameters(self) -> None:
        url = (
            "https://prrpeu3.com/launcher?gamename=rp_235&operator=rubyplay.com"
            "&server_url=https://srv.prrpeu3.com&currency=EUR&currency=USD"
            "&mode=fun&lang=en"
        )
        with self.assertRaisesRegex(ValueError, "currency.*no unívoco"):
            parse_launcher_url(url)

    def test_client_contract_is_discovered_without_game_name_routing(self) -> None:
        bundle = (
            'tt.VERSION=2,tt.BYTE_SIZE=8;'
            'tt.__class="com.gongxigames.math.core.binary.BinarySerializer";'
            'var U=class{static MATH_VERSION_$LI$(){return U.MATH_VERSION==null&&'
            '(U.MATH_VERSION=2026030300+U.RTP),U.MATH_VERSION}};'
            'var ne=U;ne.RTP=96,ne.WAGER=2;'
            'var mt={};mt._$wrappers=['
            'new uc(0,"INIT","init",x),new uc(1,"SPIN","spin",x),'
            'new uc(3,"RESPIN","respin",x),'
            'new uc(10,"BUY_FEATURE","buy_feature",x)];'
            'getBuyFeatureType(e){return e.getAction()===10?'
            'mt._$wrappers[3].getName():'
            'this.isBuyFeatureSession()?mt._$wrappers[3].getName():"none"}'
            'isBuyFeatureGame(){return!0}'
            'kl.BUY_FEATURE_FREESPIN_COST_IN_TIMES_BET=50;'
        )
        profile = discover_client_profile(
            [("https://cdn.example/game.js", bundle)]
        )
        self.assertEqual(profile.protocol_version, 2)
        self.assertEqual(profile.math_version, 2026030396)
        self.assertEqual(profile.rtp, 96.0)
        self.assertEqual(profile.wager, 2.0)
        self.assertEqual(profile.buy_feature_type, "respin")
        self.assertEqual(profile.buy_feature_multiplier, 50.0)
        self.assertTrue(profile.buy_feature_client_supported)
        self.assertIn("spin", profile.actions)
        self.assertIn("buy_feature", profile.actions)

    def test_legacy_client_without_session_buy_api_suppresses_false_server_flag(self) -> None:
        bundle = (
            'qe.VERSION=2;'
            'qe.__class="com.gongxigames.math.core.binary.BinarySerializer";'
            'var be={};be.MATH_VERSION=20220126,be.WAGER=10;'
            'var Yo=class extends GS{constructor(){super(be.MATH_VERSION),this.x=1}'
            'getMaxWager(){return be.WAGER}createSession(){return new Ra}};'
            'Yo.__class="com.gongxigames.math.arabiansecret.engine.ArabianSecretEngine";'
            'Ec.initBinaryFactory(new Yo);'
            'var GF=class{static createProxy(){return new xS(new Yo)}};'
            'isBuyFeatureGame(){return!!this.getSession().isBuyFeatureGame?.()}'
        )
        profile = discover_client_profile(
            [("https://cdn.example/legacy-game.js", bundle)]
        )
        self.assertEqual(profile.protocol_version, 2)
        self.assertEqual(profile.math_version, 20220126)
        self.assertEqual(profile.wager, 10.0)
        self.assertIs(profile.buy_feature_client_supported, False)
        self.assertIn("client.buy-feature.contract=absent", profile.evidence)

        runtime = RubyPlayRuntime(
            session=_FakeSession(),  # type: ignore[arg-type]
            launcher=LauncherConfig(
                launcher_url="https://launcher.example/launcher?x=1",
                gamename="rp_legacy",
                operator="rubyplay.com",
                server_url="https://srv.example",
                currency="EUR",
                mode="fun",
                lang="en",
            ),
            client_profile=profile,
            session_key="secret",
            fun_mode_data={"gameId": 71},
            init_data={"data": {"buy_feature_available": True}},
            bets=[1, 10, 100],
            default_bet=10,
            default_bet_index=1,
            currency="EUR",
            subunit=100,
            action_number=0,
            next_action="spin",
        )
        _apply_effective_buy_capability(runtime)
        body = runtime.init_data["data"]
        self.assertFalse(body["buy_feature_available"])
        self.assertTrue(body["buy_feature_available_server"])
        self.assertEqual(
            body["buy_feature_available_effective_reason"],
            "client_session_contract_absent",
        )

    def test_bets_and_purchase_price_are_derived_from_init(self) -> None:
        init = {
            "data": {
                "game_config": {
                    "bets": [1, 2, 3, 5, 10, 50, 100],
                    "def_bet_index": 5,
                },
                "player": {
                    "balance": 1000000,
                    "currency": "EUR",
                    "subunit": 100,
                },
            }
        }
        plan = bet_plan_from_init(init, wager=2.0)
        self.assertEqual(plan.allowed_bets, [1, 2, 3, 5, 10, 50, 100])
        self.assertEqual(plan.default_bet, 50)
        self.assertEqual(plan.effective_stake, 100.0)
        self.assertEqual(
            purchase_price(
                plan.default_bet,
                wager=plan.wager,
                multiplier=50.0,
            ),
            5000,
        )

    def test_action_number_and_server_next_action_drive_state(self) -> None:
        session = _FakeSession()
        runtime = RubyPlayRuntime(
            session=session,  # type: ignore[arg-type]
            launcher=LauncherConfig(
                launcher_url="https://launcher.example/launcher?x=1",
                gamename="rp_235",
                operator="rubyplay.com",
                server_url="https://srv.example",
                currency="EUR",
                mode="fun",
                lang="en",
            ),
            client_profile=RubyPlayClientProfile(
                protocol_version=2,
                math_version=2026030396,
            ),
            session_key="secret",
            fun_mode_data={"gameId": 235},
            init_data={},
            bets=[1, 50, 100],
            default_bet=50,
            default_bet_index=1,
            currency="EUR",
            subunit=100,
            action_number=7,
            next_action="spin",
        )
        response, request, data, previous_an = post_action(
            runtime,
            "spin",
            timeout_s=5,
            bet=50,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request["an"], 7)
        self.assertEqual(runtime.action_number, 8)
        self.assertEqual(runtime.next_action, "spin")
        self.assertEqual(
            validate_action_response(
                data,
                action="spin",
                previous_an=previous_an,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
