from __future__ import annotations

import unittest
from unittest.mock import Mock

import requests

from tester_spin.providers.bgaming import BGamingProvider  # noqa: F401
from tester_spin.providers.bgaming import hyperhive
from tester_spin.providers.bgaming.hyperhive_wire import analyze_engine_wire
from tester_spin.providers.bgaming.runtime import BGamingRuntime


class _Response:
    def __init__(self, *, text: str, url: str, status: int = 200) -> None:
        self.text = text
        self.url = url
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class HyperHiveInnerDiscoveryTests(unittest.TestCase):
    def test_engine_contract_is_discovered_from_live_inner_iframe(self) -> None:
        outer = "https://game.demo.bgaming-network.com/hyperhive?launch_token=outer"
        client = "https://game.demo.bgaming-network.com/?token=fresh-play-token"
        script_url = "https://game.demo.bgaming-network.com/assets/game-client.abc.js"
        client_js = (
            'a={id:"x",jsonrpc:"2.0",method:"play",params:{token:t,req:{bet:s,bet_type:"bet"},state_lock:""}};'
            'r.formattedRequest.params.isNormalBuy=false;'
            'r.formattedRequest.params.isSuperBuy=false;'
            'r.formattedRequest.params.action=r.type;'
            'r.formattedRequest.params.exponent=e.getCurrencyMaxExponent();'
            'a.params.req.custom_req=r.formattedRequest.params;'
        )

        def get(url, **_kwargs):
            if url == client:
                return _Response(
                    text='<html><script src="/assets/game-client.abc.js"></script></html>',
                    url=client,
                )
            if url == script_url:
                return _Response(text=client_js, url=script_url)
            return _Response(text="", url=url, status=404)

        session = requests.Session()
        session.get = Mock(side_effect=get)
        runtime = BGamingRuntime(
            session=session,
            launch_url=outer,
            api_url="https://unused.example/api",
            identifier="Game",
            csrf_header_name="X-CSRF-Token",
            csrf_header_value="csrf",
            options={"play_token": "fresh-play-token"},
            round_series_id=1,
            script_urls=[],
        )

        contract = hyperhive._download_engine_contract(runtime, timeout_s=1)
        profile = analyze_engine_wire(contract)

        self.assertIn(script_url, runtime.script_urls)
        self.assertIn("custom_req", contract)
        self.assertTrue(profile.custom_req)
        self.assertTrue(profile.custom_action)
        self.assertTrue(profile.custom_exponent)
        self.assertEqual(
            profile.custom_literals,
            {"isNormalBuy": False, "isSuperBuy": False},
        )


if __name__ == "__main__":
    unittest.main()
