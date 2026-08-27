from __future__ import annotations

import unittest

from tester_spin.providers.pragmatic_catalog_preloaded import _delta, _keys


class PragmaticPreloadedCatalogTests(unittest.TestCase):
    def test_delta_only_returns_structurally_new_game_cards(self) -> None:
        base = "https://www.pragmaticplay.com/en/games/"
        candy = {
            "text": "Candy Rush",
            "image": {
                "src": "https://www.pragmaticplay.com/wp-content/uploads/2026/05/Candy-Rush_339x180_EN.png",
                "alt": "Candy Rush",
                "attrs": {},
            },
            "links": [],
            "card_attrs": [],
        }
        sugar = {
            "text": "Sugar Rush 1000",
            "image": {
                "src": "https://www.pragmaticplay.com/wp-content/uploads/2026/01/Sugar-Rush-1000_339x180_EN.png",
                "alt": "Sugar Rush 1000",
                "attrs": {},
            },
            "links": [],
            "card_attrs": [],
        }

        before = _keys([candy], base)
        delta = _delta([candy, sugar], before, base)

        self.assertEqual(len(delta), 1)
        self.assertEqual(delta[0]["text"], "Sugar Rush 1000")


if __name__ == "__main__":
    unittest.main()
