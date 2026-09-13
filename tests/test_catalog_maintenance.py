from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tester_spin.catalog_maintenance import purge_provider_catalog_artifacts
from tester_spin.models import Game, GameTestResult
from tester_spin.storage import Storage


class CatalogMaintenanceTests(unittest.TestCase):
    def test_manual_exclusion_removes_and_blocks_reinsertion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage = Storage(Path(temp) / "tester.sqlite3")
            storage.upsert_games(
                [
                    Game("pragmatic", "good", "Good", "https://example/good"),
                    Game("pragmatic", "false-positive", "False", "https://example/false"),
                ]
            )

            blocked = storage.exclude_games(
                "pragmatic",
                {"false-positive"},
                reason="manual_gui",
            )
            self.assertEqual(blocked, 1)
            self.assertIsNone(storage.get_game("pragmatic", "false-positive"))
            self.assertEqual(
                storage.list_catalog_exclusions("pragmatic"),
                {"false-positive"},
            )

            inserted = storage.upsert_games(
                [
                    Game("pragmatic", "false-positive", "False Again", "https://example/false"),
                    Game("pragmatic", "new-good", "New Good", "https://example/new"),
                ]
            )
            self.assertEqual(inserted, 1)
            self.assertIsNone(storage.get_game("pragmatic", "false-positive"))
            self.assertIsNotNone(storage.get_game("pragmatic", "new-good"))

    def test_full_catalog_clear_can_reset_exclusions_but_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "tester.sqlite3"
            storage = Storage(db)
            storage.upsert_games(
                [Game("belatra", "one", "One", "https://example/one")]
            )
            storage.record_result(
                GameTestResult(
                    provider="belatra",
                    slug="one",
                    game_name="One",
                    game_url="https://example/one",
                    requested_spins=1,
                    successful_spins=1,
                    failed_spins=0,
                    status="OK",
                )
            )
            storage.exclude_games("belatra", {"bad"}, reason="manual_gui")

            removed, exclusions = storage.clear_provider_catalog(
                "belatra",
                clear_exclusions=True,
            )
            self.assertEqual(removed, 1)
            self.assertEqual(exclusions, 1)
            self.assertEqual(storage.list_games("belatra"), [])
            self.assertEqual(storage.list_catalog_exclusions("belatra"), set())

            with closing(sqlite3.connect(db)) as con:
                history = con.execute(
                    "SELECT COUNT(*) FROM test_results WHERE provider=?",
                    ("belatra",),
                ).fetchone()[0]
            self.assertEqual(history, 1)

    def test_artifact_purge_preserves_tests_and_runtime_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "providers" / "pragmatic"
            game = root / "False Positive"
            tests = game / "tests" / "2026-09-08"
            tests.mkdir(parents=True)
            (tests / "result.json").write_text("{}", encoding="utf-8")
            (game / "game.json").write_text("{}", encoding="utf-8")
            (game / "thumbnail.webp").write_bytes(b"thumb")
            (game / "runtime-note.json").write_text("{}", encoding="utf-8")
            (root / "catalog.json").write_text("{}", encoding="utf-8")
            (root / "catalog-pages").mkdir(parents=True)
            (root / "catalog-pages" / "page-001.html").write_text(
                "<html></html>",
                encoding="utf-8",
            )
            (root / "catalog-diagnostics").mkdir(parents=True)
            (root / "catalog-diagnostics" / "diag.json").write_text(
                "{}",
                encoding="utf-8",
            )

            stats = purge_provider_catalog_artifacts(root)

            self.assertGreaterEqual(stats.files_removed, 2)
            self.assertFalse((root / "catalog.json").exists())
            self.assertFalse((root / "catalog-pages").exists())
            self.assertFalse((root / "catalog-diagnostics").exists())
            self.assertTrue((game / "game.json").exists())
            metadata = json.loads((game / "game.json").read_text(encoding="utf-8"))
            self.assertTrue(metadata.get("catalog_recovery_disabled"))
            self.assertFalse((game / "thumbnail.webp").exists())
            self.assertTrue((tests / "result.json").exists())
            self.assertTrue((game / "runtime-note.json").exists())


if __name__ == "__main__":
    unittest.main()
