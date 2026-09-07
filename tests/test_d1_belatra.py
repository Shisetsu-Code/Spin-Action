from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tester_spin.d1_storage import D1Storage, D1StorageConfig
from tester_spin.providers.belatra import BelatraProvider


class D1StorageConfigTests(unittest.TestCase):
    def test_environment_config_accepts_explicit_tester_spin_token(self) -> None:
        env = {
            "TESTER_SPIN_D1_ACCOUNT_ID": "acc123",
            "TESTER_SPIN_D1_DATABASE_ID": "db-uuid",
            "TESTER_SPIN_D1_API_TOKEN": "secret",
        }
        with patch.dict(os.environ, env, clear=True):
            config = D1StorageConfig.from_environment()
        self.assertEqual(config.account_id, "acc123")
        self.assertEqual(config.database_id, "db-uuid")
        self.assertEqual(config.api_token, "secret")

    def test_environment_config_accepts_cloudflare_token_fallback(self) -> None:
        env = {
            "TESTER_SPIN_D1_ACCOUNT_ID": "acc123",
            "TESTER_SPIN_D1_DATABASE_ID": "db-uuid",
            "CLOUDFLARE_API_TOKEN": "secret2",
        }
        with patch.dict(os.environ, env, clear=True):
            config = D1StorageConfig.from_environment()
        self.assertEqual(config.api_token, "secret2")

    def test_environment_config_rejects_missing_fields(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                D1StorageConfig.from_environment()
        self.assertIn("TESTER_SPIN_D1_ACCOUNT_ID", str(ctx.exception))
        self.assertIn("TESTER_SPIN_D1_DATABASE_ID", str(ctx.exception))

    def test_d1_param_normalizes_bool(self) -> None:
        self.assertEqual(D1Storage._param(True), 1)
        self.assertEqual(D1Storage._param(False), 0)
        self.assertIsNone(D1Storage._param(None))


class BelatraCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.provider = BelatraProvider(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_extract_catalog_page_uses_game_links_and_image_alt(self) -> None:
        provider = self.provider
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
        games = provider._extract_catalog_page(html, "https://belatragames.com/en/games")
        self.assertEqual([game.slug for game in games], ["big-wild-buffalo-2", "just-a-bingo"])
        self.assertEqual(games[0].name, "Big Wild Buffalo 2")
        self.assertEqual(
            games[0].thumbnail_url,
            "https://belatragames.com/media/buffalo.webp",
        )

    def test_resolve_demo_url_uses_observed_free_slot_url(self) -> None:
        provider = self.provider
        game = provider._extract_catalog_page(
            '<a href="/en/games/game/just-a-bingo"><img alt="Just a Bingo"></a>',
            "https://belatragames.com/en/games",
        )[0]
        html = (
            '<script>window.demo="https://free-slot.belatragames.com/play/just-a-bingo";</script>'
        )
        self.assertEqual(
            provider._resolve_demo_url(game, html),
            "https://free-slot.belatragames.com/play/just-a-bingo",
        )

    def test_resolve_demo_url_has_safe_slug_fallback(self) -> None:
        provider = self.provider
        game = provider._extract_catalog_page(
            '<a href="/en/games/game/just-a-bingo"><img alt="Just a Bingo"></a>',
            "https://belatragames.com/en/games",
        )[0]
        self.assertEqual(
            provider._resolve_demo_url(game, "<html></html>"),
            "https://free-slot.belatragames.com/play/just-a-bingo",
        )

    def test_successful_bootstrap_remains_partial_not_spin_ok(self) -> None:
        provider = self.provider
        game = provider._extract_catalog_page(
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

        provider._discover_demo_protocol = fake_discovery  # type: ignore[method-assign]
        result = provider.test_game(
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
