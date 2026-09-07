from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from tester_spin.models import Game
from tester_spin.providers.belatra import BelatraProvider
from tester_spin.providers.one_spin4win import OneSpin4WinProvider


class OneSpin4WinCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.provider = OneSpin4WinProvider(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_webvisor_frame_is_ignored_as_telemetry(self) -> None:
        frame = {
            "reconnects": 0,
            "resource": "events/96775560",
            "wstoken": "token",
            "query": {
                "wv-type": "6",
                "wv-check": "15827",
                "wmode": "0",
                "wv-hit": "788235634",
                "page-url": "https://belatragames.com/es/games/category/2",
            },
            "seq": 1,
            "body": [
                {
                    "type": "event",
                    "event": "sessionStart",
                    "stamp": 1,
                    "frameId": 0,
                    "data": {"recordStamp": 1788819863520, "dpr": 1},
                }
            ],
        }
        self.assertTrue(self.provider._is_webvisor_payload(frame))
        self.assertEqual(
            self.provider._extract_games_from_ws_object(
                frame,
                source_url="https://www.1spin4win.com/games",
            ),
            [],
        )

    def test_extracts_catalog_games_from_ws_payload(self) -> None:
        payload = {
            "type": "catalog",
            "data": {
                "games": [
                    {
                        "gameId": "Lucky1Spin4WinHoldAndWin",
                        "gameName": "Lucky 1spin4win Hold And Win",
                        "launchUrl": "/launch/lucky",
                        "thumbnailUrl": "/img/lucky.webp",
                    },
                    {
                        "gameId": "RetroMegaFruits",
                        "gameName": "Retro Mega Fruits",
                        "launchUrl": "https://games.example/launch/retro",
                    },
                ]
            },
        }
        games = self.provider._extract_games_from_ws_object(
            payload,
            source_url="https://casino.example/lobby",
        )
        self.assertEqual(len(games), 2)
        self.assertEqual(games[0].symbol, "Lucky1Spin4WinHoldAndWin")
        self.assertEqual(games[0].url, "https://casino.example/launch/lucky")
        self.assertEqual(games[0].thumbnail_url, "https://casino.example/img/lucky.webp")
        self.assertEqual(games[1].url, "https://games.example/launch/retro")

    def test_generic_analytics_object_is_not_mistaken_for_game(self) -> None:
        payload = {
            "event": {
                "id": "123",
                "name": "sessionStart",
                "url": "https://example.test/collect",
            }
        }
        games = self.provider._extract_games_from_ws_object(
            payload,
            source_url="https://casino.example/lobby",
        )
        self.assertEqual(games, [])

    def test_socketio_prefixed_json_is_decoded(self) -> None:
        decoded = self.provider._decode_ws_json(
            '42["catalog",{"games":[{"gameId":"ABC123","gameName":"Example"}]}]'
        )
        self.assertIsInstance(decoded, list)
        self.assertEqual(decoded[0], "catalog")

    def test_game_discovery_remains_partial_until_action_frames_are_known(self) -> None:
        game = Game(
            provider=self.provider.key,
            slug="lucky",
            name="Lucky",
            url="https://casino.example/launch/lucky",
            symbol="LuckyGame",
        )

        def fake_observe(*_args, **_kwargs):
            return (
                ["wss://games.example/session/abc"],
                [
                    {
                        "direction": "received",
                        "websocket_url": "wss://games.example/session/abc",
                        "classification": "provider_or_unknown",
                        "payload": {"kind": "text", "text": "{}"},
                    }
                ],
                "",
            )

        self.provider._observe_game_websockets = fake_observe  # type: ignore[method-assign]
        result = self.provider.test_game(
            game,
            spins=1,
            timeout_s=5.0,
            stop_event=threading.Event(),
            progress=lambda _message: None,
        )
        self.assertEqual(result.status, "PARCIAL")
        self.assertEqual(result.successful_spins, 0)
        self.assertEqual(result.attempts[0].mode_kind, "DISCOVERY_WS")
        self.assertEqual(result.attempts[0].status_code, None)
        self.assertEqual(
            result.attempts[0].endpoint,
            "wss://games.example/session/abc",
        )
        self.assertEqual(result.discovered_modes[0]["catalog_transport"], "websocket")
        self.assertFalse(result.discovered_modes[0]["provider_data_http"])


class BelatraCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.provider = BelatraProvider(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_extract_catalog_page_uses_game_links_and_image_alt(self) -> None:
        html = """
        <html><body>
          <article>
            <a href="/en/games/game/big-wild-buffalo-2">
              <img alt="Big Wild Buffalo 2" src="/media/buffalo.webp">
            </a>
          </article>
          <article>
            <a href="https://belatragames.com/en/games/game/just-a-bingo">
              <img alt="Just a Bingo" data-src="/media/bingo.png">
            </a>
          </article>
        </body></html>
        """
        games = self.provider._extract_catalog_page(html, "https://belatragames.com/en/games")
        self.assertEqual([game.slug for game in games], ["big-wild-buffalo-2", "just-a-bingo"])
        self.assertEqual(games[0].name, "Big Wild Buffalo 2")
        self.assertEqual(
            games[0].thumbnail_url,
            "https://belatragames.com/media/buffalo.webp",
        )

    def test_resolve_demo_url_uses_observed_free_slot_url(self) -> None:
        game = self.provider._extract_catalog_page(
            '<a href="/en/games/game/just-a-bingo"><img alt="Just a Bingo"></a>',
            "https://belatragames.com/en/games",
        )[0]
        html = (
            '<script>window.demo="https://free-slot.belatragames.com/play/just-a-bingo";</script>'
        )
        self.assertEqual(
            self.provider._resolve_demo_url(game, html),
            "https://free-slot.belatragames.com/play/just-a-bingo",
        )

    def test_resolve_demo_url_has_safe_slug_fallback(self) -> None:
        game = self.provider._extract_catalog_page(
            '<a href="/en/games/game/just-a-bingo"><img alt="Just a Bingo"></a>',
            "https://belatragames.com/en/games",
        )[0]
        self.assertEqual(
            self.provider._resolve_demo_url(game, "<html></html>"),
            "https://free-slot.belatragames.com/play/just-a-bingo",
        )

    def test_successful_bootstrap_remains_partial_not_spin_ok(self) -> None:
        game = self.provider._extract_catalog_page(
            '<a href="/en/games/game/just-a-bingo"><img alt="Just a Bingo"></a>',
            "https://belatragames.com/en/games",
        )[0]

        def fake_discovery(*_args, **_kwargs):
            return (
                "https://free-slot.belatragames.com/play/just-a-bingo",
                200,
                12.5,
                ["https://example.test/runtime.js"],
                ["https://example.test/spin"],
            )

        self.provider._discover_demo_protocol = fake_discovery  # type: ignore[method-assign]
        result = self.provider.test_game(
            game,
            spins=1,
            timeout_s=5.0,
            stop_event=threading.Event(),
            progress=lambda _message: None,
        )
        self.assertEqual(result.status, "PARCIAL")
        self.assertEqual(result.successful_spins, 0)
        self.assertEqual(result.attempts[0].status_code, 200)
        self.assertFalse(result.attempts[0].terminal)
        self.assertTrue(result.attempts[0].ok)


if __name__ == "__main__":
    unittest.main()
