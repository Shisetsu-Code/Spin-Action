from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.app_live import _catalog_shrink_suspicious
from tester_spin.providers.pragmatic_catalog_dom import (
    _slug_from_thumbnail,
    _slug_from_url,
    snapshot_to_game,
)
from tester_spin.providers.pragmatic_hybrid import PragmaticProvider


class PragmaticCatalogSafetyTests(unittest.TestCase):
    def test_catastrophic_catalog_shrink_is_blocked(self) -> None:
        self.assertTrue(_catalog_shrink_suspicious(702, 61))
        self.assertFalse(_catalog_shrink_suspicious(702, 650))
        self.assertFalse(_catalog_shrink_suspicious(61, 50))

    def test_data_uri_cannot_create_game_slug(self) -> None:
        self.assertEqual(
            _slug_from_thumbnail(
                "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
            ),
            "",
        )

    def test_generic_site_image_without_native_dimensions_cannot_create_slug(self) -> None:
        self.assertEqual(
            _slug_from_thumbnail(
                "https://www.pragmaticplay.com/wp-content/uploads/language-japanese.png"
            ),
            "",
        )

    def test_native_game_thumbnail_can_recover_slug(self) -> None:
        self.assertEqual(
            _slug_from_thumbnail(
                "https://www.pragmaticplay.com/wp-content/uploads/Candy-Rush_339x180_EN.png"
            ),
            "candy-rush",
        )

    def test_external_games_path_is_not_accepted(self) -> None:
        self.assertEqual(
            _slug_from_url("https://example.test/en/games/fake-game/"),
            "",
        )

    def test_thumbnail_only_recovery_does_not_borrow_navigation_text(self) -> None:
        snapshot = {
            "text": "日本語\nEnglish\nDeutsch",
            "image": {
                "src": "https://www.pragmaticplay.com/wp-content/uploads/Candy-Rush_339x180_EN.png",
                "raw_src": "",
                "alt": "",
                "title": "",
                "attrs": {},
            },
            "links": [],
            "card_attrs": [],
        }
        game = snapshot_to_game(
            snapshot,
            "https://www.pragmaticplay.com/en/games/",
        )
        self.assertIsNotNone(game)
        assert game is not None
        self.assertEqual(game.slug, "candy-rush")
        self.assertEqual(game.name, "Candy Rush")

    def test_provider_marks_data_uri_catalog_row_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = PragmaticProvider(Path(temp))
            from tester_spin.models import Game

            game = Game(
                provider="pragmatic",
                slug="fake-game",
                name="日本語",
                url="https://www.pragmaticplay.com/en/games/fake-game/",
                thumbnail_url="data:image/png;base64,AAAA",
            )
            self.assertIn(
                "data URI",
                provider.catalog_record_invalid_reason(game),
            )

    def test_recover_known_catalog_rejects_data_uri_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = PragmaticProvider(Path(temp))

            good = provider.provider_root / "Candy Rush"
            good.mkdir(parents=True)
            (good / "thumbnail.png").write_bytes(b"x")
            (good / "game.json").write_text(
                json.dumps(
                    {
                        "provider_key": "pragmatic",
                        "provider_slug": "candy-rush",
                        "human_name": "Candy Rush",
                        "page_url": "https://www.pragmaticplay.com/en/games/candy-rush/",
                        "provider_internal_id": "",
                        "thumbnail": {
                            "source_url": "https://www.pragmaticplay.com/wp-content/uploads/Candy-Rush_339x180_EN.png",
                            "local_file": "thumbnail.png",
                        },
                    }
                ),
                encoding="utf-8",
            )

            bad = provider.provider_root / "Japanese"
            bad.mkdir(parents=True)
            (bad / "game.json").write_text(
                json.dumps(
                    {
                        "provider_key": "pragmatic",
                        "provider_slug": "image-png-base64-fake",
                        "human_name": "日本語",
                        "page_url": "https://www.pragmaticplay.com/en/games/image-png-base64-fake/",
                        "provider_internal_id": "",
                        "thumbnail": {
                            "source_url": "data:image/png;base64,AAAA",
                            "local_file": "",
                        },
                    }
                ),
                encoding="utf-8",
            )

            recovered = provider._recover_catalog_games_from_artifacts()

        self.assertEqual([game.slug for game in recovered], ["candy-rush"])


if __name__ == "__main__":
    unittest.main()
