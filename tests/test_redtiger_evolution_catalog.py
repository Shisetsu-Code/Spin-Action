from __future__ import annotations

import unittest

from tester_spin.providers.redtiger.catalog import (
    parse_wp_game_record,
    provider_id_from_catalog_url,
    wp_catalog_query_params,
)


class RedTigerEvolutionCatalogTests(unittest.TestCase):
    def test_provider_id_is_derived_from_public_catalog_url(self) -> None:
        url = (
            "https://games.evolution.com/all-games/"
            "?game_provider%5B0%5D=1185&custom_sort=featured"
        )
        self.assertEqual(provider_id_from_catalog_url(url), "1185")

    def test_query_matches_observed_wordpress_catalog_shape(self) -> None:
        self.assertEqual(
            wp_catalog_query_params("1185", page=2, page_size=36, custom_sort="featured"),
            {
                "_embed": 1,
                "acf_format": "standard",
                "page": 2,
                "per_page": 36,
                "game_provider[]": "1185",
                "custom_sort": "featured",
                "only_games": 1,
            },
        )

    def test_wordpress_post_id_is_launch_identity_not_acf_game_id(self) -> None:
        payload = {
            "id": 22914,
            "slug": "nightmare-family-megaways",
            "link": "https://games.evolution.com/slots/nightmare-family-megaways/",
            "title": {"rendered": "Nightmare Family Megaways™"},
            "date": "2023-09-05T00:00:00",
            "acf": {
                "game_id": "nightmarefamilym",
                "release_year": "2023",
                "rtp": "95.86% / 94.79%",
                "volatility": "high_extreme",
                "game_provider": {"post_title": "Red Tiger"},
                "game_thumbnail": {"url": "https://games.evolution.com/thumb.png"},
            },
            "_embedded": {
                "wp:term": [
                    [{"taxonomy": "game_feature", "name": "Free Spins"}],
                    [{"taxonomy": "game_type", "name": "Slots"}],
                ]
            },
        }
        record = parse_wp_game_record(payload)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.launch_id, "22914")
        self.assertEqual(record.game.symbol, "22914")
        self.assertEqual(record.table_id, "nightmarefamilym")
        self.assertEqual(record.provider_name, "Red Tiger")
        self.assertEqual(record.game_type, "Slots")
        self.assertEqual(
            record.game.url,
            "https://games.evolution.com/slots/nightmare-family-megaways/",
        )


if __name__ == "__main__":
    unittest.main()
