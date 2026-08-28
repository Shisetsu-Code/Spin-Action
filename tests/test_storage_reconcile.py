from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.storage import Storage


class StorageReconcileTests(unittest.TestCase):
    def test_reconcile_removes_only_stale_provider_rows_and_keeps_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "tester.sqlite3"
            storage = Storage(db)
            storage.upsert_games(
                [
                    Game("pragmatic", "current", "Current", "https://example/current"),
                    Game("pragmatic", "stale", "Stale", "https://example/stale"),
                    Game("other", "foreign", "Foreign", "https://example/foreign"),
                ]
            )
            storage.record_result(
                GameTestResult(
                    provider="pragmatic",
                    slug="stale",
                    game_name="Stale",
                    game_url="https://example/stale",
                    requested_spins=1,
                    successful_spins=0,
                    failed_spins=1,
                    status="ERROR",
                    error="historical failure",
                )
            )

            removed = storage.reconcile_provider_games("pragmatic", {"current"})

            self.assertEqual(removed, 1)
            self.assertIsNotNone(storage.get_game("pragmatic", "current"))
            self.assertIsNone(storage.get_game("pragmatic", "stale"))
            self.assertIsNotNone(storage.get_game("other", "foreign"))

            with sqlite3.connect(db) as con:
                history = con.execute(
                    "SELECT COUNT(*) FROM test_results WHERE provider=? AND slug=?",
                    ("pragmatic", "stale"),
                ).fetchone()[0]
            self.assertEqual(history, 1)

    def test_empty_authoritative_catalog_can_clear_provider_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage = Storage(Path(temp) / "tester.sqlite3")
            storage.upsert_games(
                [
                    Game("pragmatic", "one", "One", "https://example/one"),
                    Game("other", "two", "Two", "https://example/two"),
                ]
            )
            removed = storage.reconcile_provider_games("pragmatic", set())
            self.assertEqual(removed, 1)
            self.assertEqual(storage.list_games("pragmatic"), [])
            self.assertEqual(len(storage.list_games("other")), 1)


if __name__ == "__main__":
    unittest.main()
