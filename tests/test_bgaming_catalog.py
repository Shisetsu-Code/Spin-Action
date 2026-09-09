from __future__ import annotations

import unittest

from tester_spin.providers.bgaming.catalog import (
    filter_records_by_game_type,
    parse_catalog_html,
)


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
<div data-catalog-card data-image="https://bgaming.com/wp-content/uploads/token.webp">
  <a href="https://bgaming.com/games/token-demo"><img alt="Token Demo"></a>
  <a href="https://demo.bgaming-network.com/games/TokenDemo/FUN?play_token=secret-session">
    Play Demo
  </a>
  <div class="game-type-text">Slots</div>
</div>
<div data-catalog-card data-image="https://bgaming.com/wp-content/uploads/roulette.webp">
  <a href="https://bgaming.com/games/american-roulette"><img alt="American Roulette"></a>
  <div class="game-type-text">Roulette</div>
  <a href="https://demo.bgaming-network.com/play/AmericanRoulette/FUN?server=demo">Play Demo</a>
</div>
"""


class BGamingCatalogTests(unittest.TestCase):
    def test_parses_demo_identifier_and_metadata(self) -> None:
        records = parse_catalog_html(HTML)
        self.assertEqual(len(records), 4)

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

    def test_filters_non_slot_families_from_slots_catalog(self) -> None:
        records = parse_catalog_html(HTML)
        accepted, rejected = filter_records_by_game_type(records, "Slots")
        self.assertEqual(len(accepted), 3)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].game.name, "American Roulette")
        self.assertEqual(rejected[0].game_type, "Roulette")

    def test_does_not_persist_ephemeral_demo_tokens(self) -> None:
        records = parse_catalog_html(HTML)
        token_demo = records[2]
        self.assertEqual(token_demo.game.symbol, "TokenDemo")
        self.assertEqual(token_demo.availability, "EPHEMERAL_DEMO")
        self.assertEqual(token_demo.demo_url, "")
        self.assertEqual(token_demo.game.url, "https://bgaming.com/games/token-demo")
        self.assertNotIn("secret-session", token_demo.game.url)

    def test_keeps_non_demo_catalog_rows_without_inventing_identifier(self) -> None:
        records = parse_catalog_html(HTML)
        future = records[1]
        self.assertEqual(future.game.slug, "future-game")
        self.assertEqual(future.game.symbol, "")
        self.assertEqual(future.availability, "COMING_SOON")
        self.assertEqual(future.game.url, "https://bgaming.com/games/future-game")


if __name__ == "__main__":
    unittest.main()
