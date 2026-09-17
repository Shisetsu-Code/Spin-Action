from __future__ import annotations

import threading
import unittest

from scripts.purchase_campaign import _execute_purchase_game
from tester_spin.models import Game, GameTestResult
from tester_spin.purchase_coverage import PURCHASE_COMPLETE


class _Provider:
    key = "synthetic"

    def __init__(self) -> None:
        self.order: list[str] = []

    def test_purchase_paths(self, game, *, timeout_s, stop_event, progress):
        self.order.append("test")
        return GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
        )

    def finalize_purchase_result(self, result, *, progress):
        self.order.append("finalize")
        result.structural_map["feature_sessions"] = {
            "complete": True,
            "session_count": 0,
            "by_parent_mode": {},
        }
        return result

    def finalize_test_result(self, result, *, progress):
        raise AssertionError("purchase campaign must not run natural sampling finalizer")

    def build_purchase_coverage(self, game, result):
        self.order.append("coverage")
        self.assert_feature_finalized(result)
        return {
            "state": PURCHASE_COMPLETE,
            "counts": {"total": 1, "complete": 1, "failed": 0, "unknown": 0},
            "options": [{"purchase_id": "PURCHASE_A"}],
        }

    @staticmethod
    def assert_feature_finalized(result) -> None:
        structural = result.structural_map if isinstance(result.structural_map, dict) else {}
        if "feature_sessions" not in structural:
            raise AssertionError("feature sessions were not finalized before coverage")


class PurchaseCampaignFeatureFinalizerTests(unittest.TestCase):
    def test_purchase_finalizer_runs_before_coverage_and_general_finalizer_is_not_used(self) -> None:
        provider = _Provider()
        game = Game(
            provider="synthetic",
            slug="synthetic",
            name="Synthetic",
            url="https://example.invalid/game",
        )

        result, coverage, runtime_failed = _execute_purchase_game(
            provider,
            game,
            timeout_s=5.0,
            stop_event=threading.Event(),
            progress=lambda _message: None,
        )

        self.assertFalse(runtime_failed)
        self.assertEqual(result.status, "OK")
        self.assertEqual(coverage["state"], PURCHASE_COMPLETE)
        self.assertEqual(provider.order, ["test", "finalize", "coverage"])


if __name__ == "__main__":
    unittest.main()
