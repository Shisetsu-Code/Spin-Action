from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tester_spin.providers.pragmatic import PragmaticProvider, _safe_human_folder


class PragmaticCatalogTests(unittest.TestCase):
    def test_extracts_human_name_link_and_highest_srcset_image(self) -> None:
        html = """
        <html><body>
          <article class="game-card">
            <a href="/en/games/harvest-moon-grave-profits/">
              <img alt="Harvest Moon – Grave Profits"
                   src="/images/small.jpg"
                   srcset="/images/small.jpg 300w, /images/native.webp 1200w">
            </a>
            <h3>Harvest Moon – Grave Profits</h3>
            <a href="/en/games/harvest-moon-grave-profits/">Play Now</a>
          </article>
        </body></html>
        """
        with tempfile.TemporaryDirectory() as temp:
            provider = PragmaticProvider(Path(temp))
            games = provider._extract_catalog_page(html, "https://www.pragmaticplay.com/en/games/")

        self.assertEqual(len(games), 1)
        game = games[0]
        self.assertEqual(game.name, "Harvest Moon – Grave Profits")
        self.assertEqual(game.slug, "harvest-moon-grave-profits")
        self.assertEqual(
            game.url,
            "https://www.pragmaticplay.com/en/games/harvest-moon-grave-profits/",
        )
        self.assertEqual(game.thumbnail_url, "https://www.pragmaticplay.com/images/native.webp")

    def test_human_folder_preserves_unicode_and_spaces(self) -> None:
        self.assertEqual(
            _safe_human_folder("Harvest Moon – Grave Profits"),
            "Harvest Moon – Grave Profits",
        )
        self.assertEqual(_safe_human_folder('Bad:Name?'), "Bad_Name_")


if __name__ == "__main__":
    unittest.main()
