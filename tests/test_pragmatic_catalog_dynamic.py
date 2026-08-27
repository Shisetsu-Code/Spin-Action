from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.providers.pragmatic import PragmaticProvider as HtmlProvider
from tester_spin.providers.pragmatic_catalog import extract_dynamic_games


class PragmaticDynamicCatalogTests(unittest.TestCase):
    def _provider(self, temp: str) -> HtmlProvider:
        return HtmlProvider(Path(temp))

    def test_extracts_game_from_html_fragment_inside_json(self) -> None:
        payload = json.dumps(
            {
                "success": True,
                "html": (
                    '<article class="game-card">'
                    '<a href="/en/games/death-dominion/?cur=USD&gamelang=en">'
                    '<img alt="Death Dominion" src="/uploads/death.webp">'
                    '</a></article>'
                ),
            }
        )
        with tempfile.TemporaryDirectory() as temp:
            games = extract_dynamic_games(
                self._provider(temp),
                payload,
                "https://www.pragmaticplay.com/en/games/",
            )

        self.assertEqual([game.slug for game in games], ["death-dominion"])
        self.assertEqual(games[0].name, "Death Dominion")
        self.assertEqual(
            games[0].thumbnail_url,
            "https://www.pragmaticplay.com/uploads/death.webp",
        )

    def test_extracts_structured_json_link_name_and_thumbnail(self) -> None:
        payload = json.dumps(
            {
                "items": [
                    {
                        "game_url": "https://www.pragmaticplay.com/en/games/sunnydaze-asylum/",
                        "game_name": "Sunnydaze Asylum",
                        "thumbnail_url": "https://cdn.example/sunny.webp",
                    }
                ]
            }
        )
        with tempfile.TemporaryDirectory() as temp:
            games = extract_dynamic_games(
                self._provider(temp),
                payload,
                "https://www.pragmaticplay.com/en/games/",
            )

        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].slug, "sunnydaze-asylum")
        self.assertEqual(games[0].name, "Sunnydaze Asylum")
        self.assertEqual(games[0].thumbnail_url, "https://cdn.example/sunny.webp")

    def test_extracts_escaped_game_url_from_generic_payload(self) -> None:
        payload = r'{"result":"https:\/\/www.pragmaticplay.com\/en\/games\/cosmic-clusters\/"}'
        with tempfile.TemporaryDirectory() as temp:
            games = extract_dynamic_games(
                self._provider(temp),
                payload,
                "https://www.pragmaticplay.com/en/games/",
            )

        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].slug, "cosmic-clusters")
        self.assertEqual(games[0].name, "Cosmic Clusters")

    def test_deduplicates_same_slug_across_html_and_json_recovery(self) -> None:
        payload = json.dumps(
            {
                "html": (
                    '<a href="/en/games/big-bass-blast/">'
                    '<img alt="Big Bass Blast" src="/blast.webp"></a>'
                ),
                "permalink": "https://www.pragmaticplay.com/en/games/big-bass-blast/",
            }
        )
        with tempfile.TemporaryDirectory() as temp:
            games = extract_dynamic_games(
                self._provider(temp),
                payload,
                "https://www.pragmaticplay.com/en/games/",
            )

        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].name, "Big Bass Blast")
        self.assertTrue(games[0].thumbnail_url.endswith("/blast.webp"))


if __name__ == "__main__":
    unittest.main()
