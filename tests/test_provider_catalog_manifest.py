from __future__ import annotations

import threading
import unittest

from scripts.provider_catalog_manifest import (
    enumerate_provider_targets,
    remaining_targets,
)
from tester_spin.models import Game


class _FakeProvider:
    key = "fake"

    def __init__(self) -> None:
        self.catalog_crawl_authoritative = True
        self.catalog_crawl_reason = ""
        self.download_calls = 0
        self.received_max_pages = None

    def _download_thumbnail(self, game: Game, progress) -> None:
        self.download_calls += 1

    def catalog_record_invalid_reason(self, game: Game) -> str:
        if not game.slug or not game.url:
            return "invalid"
        return ""

    def crawl_catalog(self, *, stop_event, progress, max_pages, on_game=None):
        self.received_max_pages = max_pages
        game = Game("fake", "alpha", "Alpha", "https://example.invalid/alpha")
        self._download_thumbnail(game, progress)
        return [game]


class ProviderCatalogManifestTests(unittest.TestCase):
    def test_enumerator_suppresses_thumbnail_downloads_in_lab(self) -> None:
        provider = _FakeProvider()
        games = enumerate_provider_targets(
            provider,
            provider_key="fake",
            requested_pages=1,
            stop_event=threading.Event(),
            progress=lambda _message: None,
        )
        self.assertEqual([game.slug for game in games], ["alpha"])
        self.assertEqual(provider.download_calls, 0)
        self.assertEqual(provider.received_max_pages, 1)

    def test_full_catalog_request_uses_large_defensive_limit(self) -> None:
        provider = _FakeProvider()
        enumerate_provider_targets(
            provider,
            provider_key="fake",
            requested_pages=0,
            stop_event=threading.Event(),
            progress=lambda _message: None,
        )
        self.assertEqual(provider.received_max_pages, 10_000)

    def test_remaining_targets_excludes_only_proven_complete_slugs(self) -> None:
        games = [
            Game("p", "alpha", "Alpha", "https://example.invalid/a"),
            Game("p", "beta", "Beta", "https://example.invalid/b"),
            Game("p", "gamma", "Gamma", "https://example.invalid/g"),
        ]
        previous = {
            "alpha": "COMPLETE",
            "beta": "UNKNOWN",
            "obsolete": "COMPLETE",
        }
        remaining = remaining_targets(games, previous)
        self.assertEqual([game.slug for game in remaining], ["beta", "gamma"])


if __name__ == "__main__":
    unittest.main()
