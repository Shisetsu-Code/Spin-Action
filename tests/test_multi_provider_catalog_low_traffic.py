from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from tester_spin.models import Game
from tester_spin.multi_provider_catalog import run_provider_catalogs
from tester_spin.providers.base import ProviderAdapter
from tester_spin.storage import Storage


class _AssetHeavyProvider(ProviderAdapter):
    key = "asset-heavy"
    display_name = "Asset Heavy"
    catalog_url = "https://example.invalid/catalog"

    def __init__(self) -> None:
        self.download_calls = 0
        self.seen_max_pages: list[int] = []

    def _download_thumbnail(self, game: Game, progress) -> None:
        self.download_calls += 1

    def crawl_catalog(self, *, stop_event, progress, max_pages=100, on_game=None):
        self.seen_max_pages.append(int(max_pages))
        game = Game(
            provider=self.key,
            slug="alpha",
            name="Alpha",
            url="https://example.invalid/games/alpha",
        )
        self._download_thumbnail(game, progress)
        if on_game is not None:
            on_game(game)
        return [game]

    def test_game(self, game, *, spins, timeout_s, stop_event, progress):
        raise NotImplementedError


class MultiProviderCatalogLowTrafficTests(unittest.TestCase):
    def test_batch_catalog_suppresses_catalog_asset_downloads(self) -> None:
        provider = _AssetHeavyProvider()
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tester-spin.sqlite3")
            result = run_provider_catalogs(
                [provider],
                storage=storage,
                workers=1,
                max_pages=0,
                progress=lambda _provider, _message: None,
            )[0]

        self.assertEqual(result.status, "OK")
        self.assertEqual(provider.download_calls, 0)
        self.assertEqual(provider.seen_max_pages, [10_000])


if __name__ == "__main__":
    unittest.main()
