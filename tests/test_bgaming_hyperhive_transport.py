from __future__ import annotations

import unittest
from unittest.mock import Mock

import requests

# Importing the provider installs the wire adapter first and the transport
# adapter second, matching the production application wiring.
from tester_spin.providers.bgaming import BGamingProvider  # noqa: F401
from tester_spin.providers.bgaming import hyperhive
from tester_spin.providers.bgaming.hyperhive_transport import hyperhive_client_url
from tester_spin.providers.bgaming.runtime import BGamingRuntime


class _Response:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"id": "rpc", "jsonrpc": "2.0", "result": {}}


class BGamingHyperHiveTransportTests(unittest.TestCase):
    def _runtime(self) -> BGamingRuntime:
        session = requests.Session()
        session.get = Mock(return_value=_Response())
        session.post = Mock(return_value=_Response())
        return BGamingRuntime(
            session=session,
            launch_url=(
                "https://the-godfather3-pillars-of-power.demo.bgaming-network.com/"
                "hyperhive?launch_token=outer-launch-token"
            ),
            api_url="https://unused.example/api/session",
            identifier="TheGodfather3PillarsOfPower",
            csrf_header_name="X-CSRF-Token",
            csrf_header_value="csrf",
            options={"play_token": "play-token-value"},
            round_series_id=1,
        )

    def test_client_url_matches_har_observed_iframe(self) -> None:
        runtime = self._runtime()
        self.assertEqual(
            hyperhive_client_url(runtime),
            "https://the-godfather3-pillars-of-power.demo.bgaming-network.com/"
            "?token=play-token-value",
        )

    def test_rpc_hydrates_inner_iframe_and_uses_it_as_referer(self) -> None:
        runtime = self._runtime()
        outer = runtime.launch_url
        client = hyperhive_client_url(runtime)

        _response, _payload, data = hyperhive._rpc(
            runtime,
            "init",
            timeout_s=1,
            params={"token": "play-token-value"},
            rpc_id="rpc",
        )

        self.assertEqual(data["result"], {})
        runtime.session.get.assert_called_once()
        get_args, get_kwargs = runtime.session.get.call_args
        self.assertEqual(get_args[0], client)
        self.assertEqual(get_kwargs["headers"]["Referer"], outer)

        runtime.session.post.assert_called_once()
        post_args, post_kwargs = runtime.session.post.call_args
        self.assertEqual(
            post_args[0],
            "https://the-godfather3-pillars-of-power.demo.bgaming-network.com/api",
        )
        self.assertEqual(post_kwargs["headers"]["Referer"], client)
        self.assertEqual(
            post_kwargs["headers"]["Origin"],
            "https://the-godfather3-pillars-of-power.demo.bgaming-network.com",
        )
        self.assertEqual(runtime.launch_url, outer)

    def test_non_hyperhive_launch_is_unchanged(self) -> None:
        runtime = self._runtime()
        runtime.launch_url = "https://demo.bgaming-network.com/games/Game/FUN"
        self.assertEqual(hyperhive_client_url(runtime), runtime.launch_url)


if __name__ == "__main__":
    unittest.main()
