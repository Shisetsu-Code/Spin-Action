from __future__ import annotations

import os
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
    def test_extract_catalog_page_uses_game_links_and_image_alt(self) -> None:
        provider = BelatraProvider(Path("."))
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
        provider = BelatraProvider(Path("."))
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
        provider = BelatraProvider(Path("."))
        game = provider._extract_catalog_page(
            '<a href="/en/games/game/just-a-bingo"><img alt="Just a Bingo"></a>',
            "https://belatragames.com/en/games",
        )[0]
        self.assertEqual(
            provider._resolve_demo_url(game, "<html></html>"),
            "https://free-slot.belatragames.com/play/just-a-bingo",
        )


if __name__ == "__main__":
    unittest.main()
