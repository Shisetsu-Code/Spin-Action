from __future__ import annotations

import argparse
import tempfile
import threading
import unittest
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.purchase_coverage import PURCHASE_COMPLETE, PURCHASE_UNKNOWN
from scripts.purchase_campaign import (
    DEFAULT_PURCHASE_PROVIDERS,
    expand_provider_selection,
    run_selected_games,
)


class _FakeProvider:
    key = "fake"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def test_purchase_paths(self, game, *, timeout_s, stop_event, progress):
        self.seen.append(game.slug)
        if game.slug == "boom":
            raise RuntimeError("synthetic launcher failure")
        return GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
            symbol=game.symbol,
            run_dir="",
        )

    def finalize_test_result(self, result, *, progress):
        return result

    def build_purchase_coverage(self, game, result):
        return {
            "state": PURCHASE_COMPLETE,
            "provider": self.key,
            "game_slug": game.slug,
            "counts": {"total": 1, "complete": 1, "failed": 0, "unknown": 0},
            "options": [{"purchase_id": "P1"}],
        }


class PurchaseCampaignTests(unittest.TestCase):
    def test_default_provider_set_excludes_bgaming(self) -> None:
        self.assertEqual(
            DEFAULT_PURCHASE_PROVIDERS,
            ("pragmatic", "belatra", "1spin4win", "rubyplay", "redtiger"),
        )
        self.assertNotIn("bgaming", DEFAULT_PURCHASE_PROVIDERS)

    def test_all_expands_to_approved_purchase_provider_set(self) -> None:
        self.assertEqual(expand_provider_selection("all"), list(DEFAULT_PURCHASE_PROVIDERS))
        self.assertEqual(expand_provider_selection("pragmatic"), ["pragmatic"])
        with self.assertRaises(ValueError):
            expand_provider_selection("bgaming")

    def test_batch_continues_after_individual_game_failure_and_fails_closed(self) -> None:
        games = [
            Game(provider="fake", slug="ok-1", name="OK 1", url="https://example/1"),
            Game(provider="fake", slug="boom", name="Boom", url="https://example/2"),
            Game(provider="fake", slug="ok-2", name="OK 2", url="https://example/3"),
        ]
        provider = _FakeProvider()
        with tempfile.TemporaryDirectory() as temp:
            rows = run_selected_games(
                provider,
                games,
                timeout_s=5.0,
                stop_event=threading.Event(),
                output_dir=Path(temp),
                progress=lambda _message: None,
            )
        self.assertEqual(provider.seen, ["ok-1", "boom", "ok-2"])
        self.assertEqual([row["coverage"]["state"] for row in rows], [
            PURCHASE_COMPLETE,
            PURCHASE_UNKNOWN,
            PURCHASE_COMPLETE,
        ])
        self.assertIn("synthetic launcher failure", rows[1]["runtime_error"])

    def test_error_before_authoritative_inventory_never_becomes_no_purchase(self) -> None:
        provider = _FakeProvider()
        game = Game(provider="fake", slug="boom", name="Boom", url="https://example/2")
        with tempfile.TemporaryDirectory() as temp:
            rows = run_selected_games(
                provider,
                [game],
                timeout_s=5.0,
                stop_event=threading.Event(),
                output_dir=Path(temp),
                progress=lambda _message: None,
            )
        self.assertEqual(rows[0]["coverage"]["state"], PURCHASE_UNKNOWN)
        self.assertFalse(rows[0]["coverage"].get("no_purchase_proven", False))


if __name__ == "__main__":
    unittest.main()
