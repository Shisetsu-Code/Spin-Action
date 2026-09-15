from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from tester_spin.models import Game
from tester_spin.multi_provider_catalog import (
    DEFAULT_PROVIDER_KEYS,
    run_provider_catalogs,
)
from tester_spin.providers.base import ProviderAdapter
from tester_spin.storage import Storage


class _FakeProvider(ProviderAdapter):
    display_name = "Fake"
    catalog_url = "https://example.invalid/catalog"

    def __init__(
        self,
        key: str,
        *,
        barrier: threading.Barrier | None = None,
        fail: bool = False,
    ) -> None:
        self.key = key
        self.display_name = key
        self._barrier = barrier
        self._fail = fail
        self.seen_max_pages: list[int] = []
        self.stop_event_ids: list[int] = []

    def crawl_catalog(self, *, stop_event, progress, max_pages=100, on_game=None):
        self.seen_max_pages.append(int(max_pages))
        self.stop_event_ids.append(id(stop_event))
        progress("crawl-start")
        if self._barrier is not None:
            self._barrier.wait(timeout=2.0)
        if self._fail:
            raise RuntimeError(f"{self.key}-boom")
        game = Game(
            provider=self.key,
            slug=f"{self.key}-game",
            name=f"{self.key} Game",
            url=f"https://example.invalid/{self.key}-game",
        )
        if on_game is not None:
            on_game(game)
        return [game]

    def test_game(self, game, *, spins, timeout_s, stop_event, progress):
        raise NotImplementedError


class MultiProviderCatalogTests(unittest.TestCase):
    def test_default_batch_excludes_bgaming(self) -> None:
        self.assertEqual(
            DEFAULT_PROVIDER_KEYS,
            ("pragmatic", "1spin4win", "belatra", "rubyplay", "redtiger"),
        )
        self.assertNotIn("bgaming", DEFAULT_PROVIDER_KEYS)

    def test_full_catalog_runs_providers_in_parallel_and_persists_results(self) -> None:
        barrier = threading.Barrier(2)
        alpha = _FakeProvider("alpha", barrier=barrier)
        beta = _FakeProvider("beta", barrier=barrier)

        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tester-spin.sqlite3")
            results = run_provider_catalogs(
                [alpha, beta],
                storage=storage,
                workers=2,
                max_pages=0,
                progress=lambda _provider, _message: None,
            )

            self.assertEqual([item.provider for item in results], ["alpha", "beta"])
            self.assertTrue(all(item.status == "OK" for item in results))
            self.assertEqual(alpha.seen_max_pages, [10_000])
            self.assertEqual(beta.seen_max_pages, [10_000])
            self.assertNotEqual(alpha.stop_event_ids, beta.stop_event_ids)
            self.assertEqual(len(storage.list_games("alpha")), 1)
            self.assertEqual(len(storage.list_games("beta")), 1)

    def test_provider_failure_does_not_cancel_other_provider(self) -> None:
        broken = _FakeProvider("broken", fail=True)
        healthy = _FakeProvider("healthy")

        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tester-spin.sqlite3")
            results = run_provider_catalogs(
                [broken, healthy],
                storage=storage,
                workers=2,
                max_pages=3,
                progress=lambda _provider, _message: None,
            )

            by_provider = {item.provider: item for item in results}
            self.assertEqual(by_provider["broken"].status, "ERROR")
            self.assertIn("broken-boom", by_provider["broken"].error)
            self.assertEqual(by_provider["healthy"].status, "OK")
            self.assertEqual(healthy.seen_max_pages, [3])
            self.assertEqual(len(storage.list_games("healthy")), 1)

    def test_full_non_authoritative_crawl_is_partial_and_never_reconciles_old_rows(self) -> None:
        provider = _FakeProvider("partial")
        old = Game(
            provider="partial",
            slug="old-game",
            name="Old Game",
            url="https://example.invalid/old-game",
        )

        def partial_crawl(*, stop_event, progress, max_pages=100, on_game=None):
            provider.set_catalog_authority(False, "source incomplete")
            return _FakeProvider.crawl_catalog(
                provider,
                stop_event=stop_event,
                progress=progress,
                max_pages=max_pages,
                on_game=on_game,
            )

        provider.crawl_catalog = partial_crawl  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tester-spin.sqlite3")
            storage.upsert_games([old])
            results = run_provider_catalogs(
                [provider],
                storage=storage,
                workers=1,
                max_pages=0,
                progress=lambda _provider, _message: None,
            )

            self.assertEqual(results[0].status, "PARCIAL")
            self.assertFalse(results[0].authoritative)
            self.assertIn("source incomplete", results[0].authority_reason)
            slugs = {game.slug for game in storage.list_games("partial")}
            self.assertEqual(slugs, {"old-game", "partial-game"})


if __name__ == "__main__":
    unittest.main()
