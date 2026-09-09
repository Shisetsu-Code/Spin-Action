from __future__ import annotations

import unittest

from tester_spin.providers.bgaming.catalog import parse_catalog_html


HTML = """
<div data-catalog-card
     data-image="https://bgaming.com/wp-content/uploads/sample.webp">
  <a href="https://bgaming.com/games/stars-stripes-hold-and-win">
    <img alt="Stars &amp; Stripes Hold and Win">
  </a>
  <div class="bottom_container">
    <div><p>Rtp</p><p class="paragraph-102">96.70</p><p class="paragraph-102">%</p></div>
    <div><p>Volatility</p><p class="paragraph-98">Very-high</p></div>
  </div>
  <div class="game-type-text">Slots</div>
  <a href="https://demo.bgaming-network.com/play/StarsAndStripesHoldAndWin/FUN?server=demo">
    Play Demo
  </a>
</div>
<div data-catalog-card data-image="https://bgaming.com/wp-content/uploads/soon.webp">
  <a href="https://bgaming.com/games/future-game"><img alt="Future Game"></a>
  <div>Coming soon</div>
  <div class="game-type-text">Slots</div>
</div>
"""


class BGamingCatalogTests(unittest.TestCase):
    def test_parses_demo_identifier_and_metadata(self) -> None:
        records = parse_catalog_html(HTML)
        self.assertEqual(len(records), 2)

        first = records[0]
        self.assertEqual(first.game.provider, "bgaming")
        self.assertEqual(first.game.slug, "stars-stripes-hold-and-win")
        self.assertEqual(first.game.name, "Stars & Stripes Hold and Win")
        self.assertEqual(first.game.symbol, "StarsAndStripesHoldAndWin")
        self.assertIn("/play/StarsAndStripesHoldAndWin/FUN", first.game.url)
        self.assertEqual(first.rtp, 96.70)
        self.assertEqual(first.volatility, "Very-high")
        self.assertEqual(first.game_type, "Slots")
        self.assertEqual(first.availability, "DEMO")

    def test_keeps_non_demo_catalog_rows_without_inventing_identifier(self) -> None:
        records = parse_catalog_html(HTML)
        future = records[1]
        self.assertEqual(future.game.slug, "future-game")
        self.assertEqual(future.game.symbol, "")
        self.assertEqual(future.availability, "COMING_SOON")
        self.assertEqual(future.game.url, "https://bgaming.com/games/future-game")


if __name__ == "__main__":
    unittest.main()
