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

    def test_extract_catalog_page_matches_observed_webflow_har(self) -> None:
        html = """
        <html><body>
          <div class="item_portfolio is-gallery game-card-hover">
            <img
              alt="Lucky 1spin4win Hold And Win"
              class="image_portfolio-game"
              src="https://cdn.example/lucky.webp">
            <div class="hover_portfolio">
              <div class="wrap_portfolio-hover">
                <a class="link_portfolio-game w-inline-block"
                   href="/es/games/lucky-1spin4win-hold-and-win">
                  <div fs-list-field="name">Lucky 1spin4win Hold And Win</div>
                  <div class="hide" fs-list-field="slug">lucky-1spin4win-hold-and-win</div>
                </a>
                <a class="button is-small game w-button"
                   href="https://gs.1spin4win.com:10443/gmh5/games.html?game=Lucky1Spin4WinHoldAndWin&amp;currency=EUR&amp;config=1&amp;freeplay=true&amp;language=en&amp;exit=none">
                   demo
                </a>
              </div>
            </div>
          </div>
          <div role="navigation" class="w-pagination-wrapper">
            <a href="?ae0c3ebe_page=2"
               aria-label="Next Page"
               class="w-pagination-next button is-ghost">
               cargar más
            </a>
          </div>
        </body></html>
        """
        games, next_url = self.provider._extract_catalog_page(
            html,
            "https://www.1spin4win.com/es/games",
        )
        self.assertEqual(len(games), 1)
        game = games[0]
        self.assertEqual(game.slug, "lucky-1spin4win-hold-and-win")
        self.assertEqual(game.name, "Lucky 1spin4win Hold And Win")
        self.assertEqual(game.symbol, "Lucky1Spin4WinHoldAndWin")
        self.assertEqual(
            game.url,
            "https://gs.1spin4win.com:10443/gmh5/games.html?"
            "game=Lucky1Spin4WinHoldAndWin&currency=EUR&config=1&"
            "freeplay=true&language=en&exit=none",
        )
        self.assertEqual(game.thumbnail_url, "https://cdn.example/lucky.webp")
        self.assertEqual(
            next_url,
            "https://www.1spin4win.com/es/games?ae0c3ebe_page=2",
        )

    def test_demo_symbol_supports_filename_style_seen_in_har(self) -> None:
        self.assertEqual(
            self.provider._demo_symbol(
                "https://gs.1spin4win.com:10443/gmh5/"
                "luckyfoxilianholdandwin.html?currency=EUR&freeplay=true"
            ),
            "luckyfoxilianholdandwin",
        )

    def test_demo_symbol_prefers_game_query_parameter(self) -> None:
        self.assertEqual(
            self.provider._demo_symbol(
                "https://gs.1spin4win.com:10443/gmh5/games.html?"
                "game=WishAndSpinFortune&currency=EUR"
            ),
            "WishAndSpinFortune",
        )

    def test_webvisor_frame_is_still_ignored_during_game_ws_capture(self) -> None:
        frame = {
            "reconnects": 0,
            "resource": "events/96775560",
            "wstoken": "token",
            "query": {
                "wv-type": "6",
                "wv-check": "15827",
                "wv-hit": "788235634",
            },
            "body": [{"event": "sessionStart"}],
        }
        self.assertTrue(self.provider._is_webvisor_payload(frame))

    def test_game_discovery_remains_partial_until_action_frames_are_known(self) -> None:
        game = Game(
            provider=self.provider.key,
            slug="lucky",
            name="Lucky",
            url=(
                "https://gs.1spin4win.com:10443/gmh5/games.html?"
                "game=LuckyGame&freeplay=true"
            ),
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
        self.assertEqual(
            result.attempts[0].endpoint,
            "wss://games.example/session/abc",
        )
        self.assertEqual(
            result.discovered_modes[0]["catalog_transport"],
            "webflow_html",
        )


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
