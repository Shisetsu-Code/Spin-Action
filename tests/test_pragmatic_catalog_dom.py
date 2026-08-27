from __future__ import annotations

import unittest

from tester_spin.providers.pragmatic_catalog_dom import snapshot_to_game


class PragmaticVisibleDomCatalogTests(unittest.TestCase):
    def test_recovers_candy_rush_from_har_thumbnail_without_href(self) -> None:
        snapshot = {
            "text": "Candy Rush\nPlay Now",
            "image": {
                "src": "https://www.pragmaticplay.com/wp-content/uploads/2026/05/Candy-Rush_339x180_EN.png",
                "raw_src": "",
                "alt": "Candy Rush",
                "title": "",
                "attrs": {},
            },
            "links": [],
            "card_attrs": [],
        }
        game = snapshot_to_game(snapshot, "https://www.pragmaticplay.com/en/games/")
        self.assertIsNotNone(game)
        assert game is not None
        self.assertEqual(game.slug, "candy-rush")
        self.assertEqual(game.name, "Candy Rush")
        self.assertEqual(game.url, "https://www.pragmaticplay.com/en/games/candy-rush/")
        self.assertIn("Candy-Rush_339x180_EN.png", game.thumbnail_url)

    def test_prefers_real_game_href_over_thumbnail_heuristic(self) -> None:
        snapshot = {
            "text": "Harvest Moon – Grave Profits\nPlay Now",
            "image": {
                "src": "https://www.pragmaticplay.com/wp-content/uploads/x/unrelated_339x180_EN.png",
                "alt": "Harvest Moon – Grave Profits",
                "title": "",
                "attrs": {},
            },
            "links": [
                {
                    "href": "https://www.pragmaticplay.com/en/games/harvest-moon-grave-profits/?gamelang=en&cur=USD",
                    "text": "Play Now",
                    "attrs": {},
                }
            ],
            "card_attrs": [],
        }
        game = snapshot_to_game(snapshot, "https://www.pragmaticplay.com/en/games/")
        self.assertIsNotNone(game)
        assert game is not None
        self.assertEqual(game.slug, "harvest-moon-grave-profits")
        self.assertEqual(game.name, "Harvest Moon – Grave Profits")
        self.assertIn("/en/games/harvest-moon-grave-profits/", game.url)

    def test_recovers_game_url_from_data_attribute(self) -> None:
        snapshot = {
            "text": "Sugar Rush 1000",
            "image": {
                "src": "https://www.pragmaticplay.com/wp-content/uploads/2026/01/Sugar-Rush-1000_339x180_EN.png",
                "alt": "Sugar Rush 1000",
                "attrs": {},
            },
            "links": [
                {
                    "href": "",
                    "text": "Play Now",
                    "attrs": {"data-href": "/en/games/sugar-rush-1000/"},
                }
            ],
            "card_attrs": [],
        }
        game = snapshot_to_game(snapshot, "https://www.pragmaticplay.com/en/games/")
        self.assertIsNotNone(game)
        assert game is not None
        self.assertEqual(game.slug, "sugar-rush-1000")
        self.assertEqual(game.url, "https://www.pragmaticplay.com/en/games/sugar-rush-1000/")


if __name__ == "__main__":
    unittest.main()
