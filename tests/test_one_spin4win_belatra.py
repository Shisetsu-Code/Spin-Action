from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from tester_spin.providers.belatra import BelatraProvider
from tester_spin.providers.one_spin4win import OneSpin4WinProvider


class OneSpin4WinCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.provider = OneSpin4WinProvider(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_extract_catalog_page_uses_official_game_links(self) -> None:
        html = """
        <html><body>
          <article>
            <a href="/games/lucky-1spin4win-hold-and-win">
              <img alt="Lucky 1spin4win Hold And Win" src="/media/lucky.webp">
            </a>
          </article>
          <a href="/games">Portfolio</a>
        </body></html>
        """
        games = self.provider._extract_catalog_page(html, "https://www.1spin4win.com/games")
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].slug, "lucky-1spin4win-hold-and-win")
        self.assertEqual(games[0].name, "Lucky 1spin4win Hold And Win")
        self.assertEqual(
            games[0].thumbnail_url,
            "https://www.1spin4win.com/media/lucky.webp",
        )

    def test_extract_game_id_from_visible_detail_text(self) -> None:
        html = """
        <section>
          <div>Game ID</div>
          <div>Lucky1Spin4WinHoldAndWin</div>
        </section>
        """
        self.assertEqual(
            self.provider._extract_game_id(html),
            "Lucky1Spin4WinHoldAndWin",
        )

    def test_extract_demo_url_from_official_gs_host(self) -> None:
        html = """
        <a href="https://gs.1spin4win.com/demo/session/abc?lang=en">Try Game Demo!</a>
        """
        self.assertEqual(
            self.provider._extract_demo_url(html, "https://www.1spin4win.com/games/example"),
            "https://gs.1spin4win.com/demo/session/abc?lang=en",
        )

    def test_successful_bootstrap_remains_partial_not_spin_ok(self) -> None:
        game = self.provider._extract_catalog_page(
            '<a href="/games/lucky-1spin4win-hold-and-win">'
            '<img alt="Lucky 1spin4win Hold And Win"></a>',
            "https://www.1spin4win.com/games",
        )[0]

        def fake_discovery(*_args, **_kwargs):
            return (
                "https://gs.1spin4win.com/demo/session/abc",
                "Lucky1Spin4WinHoldAndWin",
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
        self.assertEqual(result.symbol, "Lucky1Spin4WinHoldAndWin")
        self.assertEqual(result.attempts[0].status_code, 200)
        self.assertTrue(result.attempts[0].ok)
        self.assertFalse(result.attempts[0].terminal)


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
