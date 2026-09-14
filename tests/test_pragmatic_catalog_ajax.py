from __future__ import annotations

import unittest

from tester_spin.models import Game
from tester_spin.providers.pragmatic_catalog_ajax import (
    AjaxPage,
    _ajax_url,
    _catalog_artifacts_enabled,
    _items_per_page,
    _looks_like_ajax_catalog,
)


class PragmaticAjaxCatalogTests(unittest.TestCase):
    def test_builds_exact_har_load_more_request_shape(self) -> None:
        url = _ajax_url("https://www.pragmaticplay.com/en/games/", 5)
        self.assertEqual(
            url,
            "https://www.pragmaticplay.com/en/games/"
            "?ajax=1&cats=undefined&lang=en&cur=USD&studio=all"
            "&device=undefined&search=&page=5",
        )

    def test_reads_official_items_per_page_hidden_input(self) -> None:
        html = '<input type="hidden" name="items-per-page" value="9" />'
        self.assertEqual(_items_per_page(html), 9)

    def test_catalog_artifact_persistence_defaults_on(self) -> None:
        class Provider:
            pass

        self.assertTrue(_catalog_artifacts_enabled(Provider()))

    def test_catalog_artifact_persistence_can_be_disabled_for_lab_enumeration(self) -> None:
        class Provider:
            catalog_persist_artifacts = False

        self.assertFalse(_catalog_artifacts_enabled(Provider()))

    def test_ajax_page_with_game_is_valid(self) -> None:
        game = Game(
            provider="pragmatic",
            slug="candy-rush",
            name="Candy Rush",
            url="https://www.pragmaticplay.com/en/games/candy-rush/",
        )
        page = AjaxPage(
            page=3,
            url="https://www.pragmaticplay.com/en/games/?ajax=1&page=3",
            status=200,
            elapsed_ms=400.0,
            games=[game],
        )
        self.assertTrue(_looks_like_ajax_catalog(page))

    def test_error_page_is_not_accepted_as_terminal_catalog_page(self) -> None:
        page = AjaxPage(
            page=2,
            url="https://www.pragmaticplay.com/en/games/?ajax=1&page=2",
            status=200,
            elapsed_ms=400.0,
            games=[],
            error="unexpected response",
        )
        self.assertFalse(_looks_like_ajax_catalog(page))


if __name__ == "__main__":
    unittest.main()
