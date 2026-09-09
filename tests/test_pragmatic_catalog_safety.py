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
from tester_spin.providers.pragmatic_catalog_preloaded import _continue_after_no_growth


class PragmaticCatalogSafetyTests(unittest.TestCase):
    def test_catastrophic_catalog_shrink_is_blocked(self) -> None:
        self.assertTrue(_catalog_shrink_suspicious(702, 61))
        self.assertFalse(_catalog_shrink_suspicious(702, 650))
        self.assertFalse(_catalog_shrink_suspicious(61, 50))

    def test_pragmatic_strict_ratio_blocks_large_but_not_catastrophic_shrink(self) -> None:
        self.assertTrue(_catalog_shrink_suspicious(702, 600, 0.90))
        self.assertFalse(_catalog_shrink_suspicious(702, 650, 0.90))

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

    def test_real_game_url_with_data_uri_image_does_not_use_language_label(self) -> None:
        snapshot = {
            "text": "日本語\nPlay Now",
            "image": {
                "src": "data:image/png;base64,AAAA",
                "raw_src": "data:image/png;base64,AAAA",
                "alt": "日本語",
                "title": "",
                "attrs": {},
            },
            "links": [
                {
                    "href": "https://www.pragmaticplay.com/en/games/candy-rush/",
                    "text": "Play Now",
                    "attrs": {},
                }
            ],
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
        self.assertEqual(game.thumbnail_url, "")

    def test_data_placeholder_uses_real_lazy_thumbnail_when_available(self) -> None:
        snapshot = {
            "text": "Candy Rush",
            "image": {
                "src": "data:image/png;base64,AAAA",
                "raw_src": "data:image/png;base64,AAAA",
                "alt": "Candy Rush",
                "title": "",
                "attrs": {
                    "data-src": "https://www.pragmaticplay.com/wp-content/uploads/Candy-Rush_339x180_EN.png"
                },
            },
            "links": [
                {
                    "href": "https://www.pragmaticplay.com/en/games/candy-rush/",
                    "text": "Play Now",
                    "attrs": {},
                }
            ],
            "card_attrs": [],
        }
        game = snapshot_to_game(
            snapshot,
            "https://www.pragmaticplay.com/en/games/",
        )
        self.assertIsNotNone(game)
        assert game is not None
        self.assertEqual(game.name, "Candy Rush")
        self.assertIn("Candy-Rush_339x180_EN.png", game.thumbnail_url)

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

    def test_preloaded_cards_do_not_force_early_fallback_stop(self) -> None:
        self.assertTrue(
            _continue_after_no_growth(
                hidden_preloaded=35,
                button_present=True,
                streak=1,
            )
        )
        self.assertTrue(
            _continue_after_no_growth(
                hidden_preloaded=35,
                button_present=True,
                streak=7,
            )
        )
        self.assertFalse(
            _continue_after_no_growth(
                hidden_preloaded=35,
                button_present=True,
                streak=8,
            )
        )
        self.assertFalse(
            _continue_after_no_growth(
                hidden_preloaded=35,
                button_present=False,
                streak=1,
            )
        )

    def test_pragmatic_provider_uses_strict_reconcile_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = PragmaticProvider(Path(temp))
            self.assertEqual(provider.min_catalog_reconcile_ratio, 0.90)

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
