from __future__ import annotations

import unittest
from unittest.mock import Mock

import requests

# Importing the provider installs the production HyperHive adapters.
from tester_spin.providers.bgaming import BGamingProvider  # noqa: F401
from tester_spin.providers.bgaming import hyperhive
from tester_spin.providers.bgaming.hyperhive_transport import (
    hyperhive_client_url,
    prepare_hyperhive_client,
)
from tester_spin.providers.bgaming.runtime import BGamingRuntime


class _Response:
    status_code = 200

    def __init__(self, *, text: str = "", url: str = "") -> None:
        self.text = text
        self.url = url

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

    def test_client_url_matches_live_iframe_shape(self) -> None:
        runtime = self._runtime()
        self.assertEqual(
            hyperhive_client_url(runtime),
            "https://the-godfather3-pillars-of-power.demo.bgaming-network.com/"
            "?token=play-token-value",
        )

    def test_prepare_inner_client_collects_game_script_urls(self) -> None:
        runtime = self._runtime()
        client = hyperhive_client_url(runtime)
        runtime.session.get.return_value = _Response(
            text=(
                '<html><head>'
                '<script src="/assets/runtime.123.js"></script>'
                '<script src="https://cdn.bgaming-network.com/game/client.456.js"></script>'
                '</head></html>'
            ),
            url=client,
        )

        result = prepare_hyperhive_client(runtime, timeout_s=1, force=True)

        self.assertEqual(result, client)
        self.assertIn(
            "https://the-godfather3-pillars-of-power.demo.bgaming-network.com/assets/runtime.123.js",
            runtime.script_urls,
        )
        self.assertIn(
            "https://cdn.bgaming-network.com/game/client.456.js",
            runtime.script_urls,
        )

    def test_loader_res_literal_resolves_versioned_bundle(self) -> None:
        runtime = self._runtime()
        loader = "https://the-godfather3-pillars-of-power.demo.bgaming-network.com/loader.js"
        text = 'const res="v2026"; loadScript("bundle.js");'

        urls = _dynamic_loader_script_urls(runtime, text, loader)

        self.assertIn(
            "https://the-godfather3-pillars-of-power.demo.bgaming-network.com/v2026/bundle.js",
            urls,
        )
    def test_loader_script_can_reveal_hash_manifests_one_level_later(self) -> None:
        runtime = self._runtime()
        client = hyperhive_client_url(runtime)
        origin = "https://the-godfather3-pillars-of-power.demo.bgaming-network.com/"
        inner_html = '<script src="/loader.js"></script>'
        loader_js = """
            var versionPath = "";
            loadScript("./clientfilesHashes.js");
            loadScript(`./game${versionPath}/gamesFilesHashes.js`);
        """
        client_hashes = 'var x=[{"fileName":"client.min.js","hash":"aaaaaaaaaaaaaaaa"}];'
        game_hashes = 'var x=[{"fileName":"game.min.js","hash":"bbbbbbbbbbbbbbbb"}];'

        def fake_get(url, *args, **kwargs):
            if url == client:
                return _Response(text=inner_html, url=client)
            if url == origin + "loader.js":
                return _Response(text=loader_js, url=url)
            if "clientfilesHashes.js" in url:
                return _Response(text=client_hashes, url=url)
            if "gamesFilesHashes.js" in url:
                return _Response(text=game_hashes, url=url)
            return _Response(text="", url=url)

        runtime.session.get.side_effect = fake_get
        prepare_hyperhive_client(runtime, timeout_s=1, force=True)

        self.assertIn(origin + "clientfilesHashes.js", runtime.script_urls)
        self.assertIn(origin + "game/gamesFilesHashes.js", runtime.script_urls)
        self.assertIn(
            origin + "client.min.js?key=aaaaaaaaaaaaaaaa",
            runtime.script_urls,
        )
        self.assertIn(
            origin + "game/game.min.js?key=bbbbbbbbbbbbbbbb",
            runtime.script_urls,
        )
    def test_dynamic_loader_resolves_live_hash_manifests_and_keyed_binaries(self) -> None:
        runtime = self._runtime()
        client = hyperhive_client_url(runtime)
        origin = "https://the-godfather3-pillars-of-power.demo.bgaming-network.com/"
        inner_html = """
            <script>
              var KEY = "key=";
              var gamePath = "";
              var versionPath = "";
              loadScript(getAbsolutePath("./clientfilesHashes.js") + "?" + KEY + Date.now(), function () {
                loadScript(getAbsolutePath(`./game${versionPath}/gamesFilesHashes.js`) + "?" + KEY + Date.now(), function () {
                  loadScript(getAbsolutePath("./common.min.js") + "?" + _get_client_hash_by_id("common.min.js"), function () {
                    loadScript(getAbsolutePath("./client.min.js") + "?" + _get_client_hash_by_id("client.min.js"), function () {});
                  });
                });
              });
            </script>
        """
        client_hashes = """
            var _clientHash_ = [
              {"fileName":"client.min.js","hash":"609d00f2da9911664be68a7db8fa706f8cef4baf"},
              {"fileName":"common.min.js","hash":"e9d2bd44d5d3952e83f1a9f5859c38f4e39b5f52"}
            ];
        """
        game_hashes = """
            var _gameHash_ = [
              {"fileName":"game.min.js","hash":"c7f6c8eedd125582dbdbda6af13c2a294f27974f"},
              {"fileName":"integration.min.js","hash":"1f4508f35fd31143fb66897bb1daba8903da43c3"}
            ];
        """

        def fake_get(url, *args, **kwargs):
            if url == client:
                return _Response(text=inner_html, url=client)
            if "clientfilesHashes.js" in url:
                return _Response(text=client_hashes, url=url)
            if "gamesFilesHashes.js" in url:
                return _Response(text=game_hashes, url=url)
            return _Response(text="", url=url)

        runtime.session.get.side_effect = fake_get
        prepare_hyperhive_client(runtime, timeout_s=1, force=True)

        self.assertIn(origin + "clientfilesHashes.js", runtime.script_urls)
        self.assertIn(origin + "game/gamesFilesHashes.js", runtime.script_urls)
        self.assertIn(
            origin + "client.min.js?key=609d00f2da9911664be68a7db8fa706f8cef4baf",
            runtime.script_urls,
        )
        self.assertIn(
            origin + "game/game.min.js?key=c7f6c8eedd125582dbdbda6af13c2a294f27974f",
            runtime.script_urls,
        )
        self.assertIn(
            origin + "game/integration.min.js?key=1f4508f35fd31143fb66897bb1daba8903da43c3",
            runtime.script_urls,
        )

    def test_transport_diagnostics_are_sanitized_and_bounded(self) -> None:
        runtime = self._runtime()
        client = hyperhive_client_url(runtime)
        runtime.session.get.return_value = _Response(
            text='<script src="/client.js"></script>',
            url=client,
        )

        prepare_hyperhive_client(runtime, timeout_s=1, force=True)

        rows = runtime.options.get("_hyperhive_transport_diagnostics")
        self.assertIsInstance(rows, list)
        self.assertTrue(rows)
        rendered = str(rows)
        self.assertNotIn("play-token-value", rendered)
        self.assertNotIn("?token=", rendered)
        self.assertTrue(
            any(row.get("kind") == "inner-client-response" for row in rows)
        )

    def test_failed_inner_get_is_not_cached_as_hydrated(self) -> None:
        runtime = self._runtime()
        client = hyperhive_client_url(runtime)
        runtime.session.get.side_effect = [
            requests.ConnectionError("temporary"),
            _Response(text='<script src="/client.js"></script>', url=client),
        ]

        with self.assertRaises(requests.ConnectionError):
            prepare_hyperhive_client(runtime, timeout_s=1, force=True)
        prepare_hyperhive_client(runtime, timeout_s=1)

        requested_urls = [call.args[0] for call in runtime.session.get.call_args_list]
        self.assertEqual(requested_urls.count(client), 2)
        self.assertIn(
            "https://the-godfather3-pillars-of-power.demo.bgaming-network.com/client.js",
            runtime.script_urls,
        )

    def test_rpc_hydrates_inner_iframe_and_uses_it_as_referer(self) -> None:
        runtime = self._runtime()
        outer = runtime.launch_url
        client = hyperhive_client_url(runtime)
        runtime.session.get.return_value = _Response(text="", url=client)

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
