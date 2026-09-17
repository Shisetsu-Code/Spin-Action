from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from tester_spin.feature_sessions import (
    enforce_complete_feature_sessions,
    finalize_feature_session_report,
    make_feature_session,
)
from tester_spin.models import Game, GameTestResult
from tester_spin.purchase_coverage import (
    PURCHASE_COMPLETE,
    PURCHASE_UNKNOWN,
    finalize_purchase_coverage,
    make_purchase_option,
)
from scripts.purchase_campaign import (
    DEFAULT_PURCHASE_PROVIDERS,
    attach_safety_limiter,
    expand_provider_selection,
    run_selected_games,
)


class _RecordingEvent:
    def __init__(self) -> None:
        self.waits: list[float] = []
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout: float) -> bool:
        self.waits.append(float(timeout))
        return self._set


class _FakeProvider:
    key = "fake"
    max_test_concurrency = None

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.finalizer_calls = 0
        self.purchase_finalizer_calls = 0
        self.limiter = None

    def effective_test_concurrency(self, requested: int) -> int:
        requested = max(1, int(requested))
        if self.max_test_concurrency is None:
            return requested
        return min(requested, max(1, int(self.max_test_concurrency)))

    def set_request_rate_limiter(self, limiter) -> None:
        self.limiter = limiter

    def set_provider_request_rate_limit(self, requests_per_minute: int) -> int:
        return self.limiter.set_requests_per_minute(requests_per_minute)

    def provider_request_rate_snapshot(self):
        return {} if self.limiter is None else self.limiter.snapshot()

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
        self.finalizer_calls += 1
        raise AssertionError("purchase campaign must not invoke general sampling/path finalizer")

    def finalize_purchase_result(self, result, *, progress):
        self.purchase_finalizer_calls += 1
        return result

    def build_purchase_coverage(self, game, result):
        return {
            "state": PURCHASE_COMPLETE,
            "provider": self.key,
            "game_slug": game.slug,
            "counts": {"total": 1, "complete": 1, "failed": 0, "unknown": 0},
            "options": [{"purchase_id": "P1"}],
        }


class _FeatureGateProvider(_FakeProvider):
    def __init__(self, run_root: Path) -> None:
        super().__init__()
        self.run_root = Path(run_root)

    def test_purchase_paths(self, game, *, timeout_s, stop_event, progress):
        self.seen.append(game.slug)
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
            run_dir=str(self.run_root),
        )

    def finalize_purchase_result(self, result, *, progress):
        self.purchase_finalizer_calls += 1
        session = make_feature_session(
            session_id="P1:1",
            trigger="PURCHASE",
            parent_mode="P1",
            attempt_number=1,
            rounds=[],
            choices=[],
            terminal_proven=False,
            returned_to_base=False,
            wire_steps=1,
            reasons=["synthetic feature remains active"],
        )
        report = finalize_feature_session_report(
            result,
            sessions=[session],
            authority="synthetic-feature-gate",
        )
        return enforce_complete_feature_sessions(result, report, progress=progress)

    def build_purchase_coverage(self, game, result):
        option = make_purchase_option(
            "P1",
            provider_selector={"id": "P1"},
            executable=True,
            wire_contract_state="PROVEN",
            execution_state="COMPLETE",
            terminal=True,
            reason="synthetic root purchase is terminal",
        )
        return finalize_purchase_coverage(
            result,
            options=[option],
            inventory_state="COMPLETE",
            authority="synthetic-root+feature-gate",
        )


class _ParallelProvider(_FakeProvider):
    def __init__(self, cap: int) -> None:
        super().__init__()
        self.max_test_concurrency = cap
        self._active = 0
        self.max_active = 0
        self._active_lock = threading.Lock()

    def test_purchase_paths(self, game, *, timeout_s, stop_event, progress):
        with self._active_lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(0.04)
            return super().test_purchase_paths(
                game,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
            )
        finally:
            with self._active_lock:
                self._active -= 1


class _AlwaysFailProvider(_FakeProvider):
    def test_purchase_paths(self, game, *, timeout_s, stop_event, progress):
        self.seen.append(game.slug)
        raise RuntimeError("HTTP 403 provider demo blocked")


class _PartialBlockedProvider(_FakeProvider):
    def test_purchase_paths(self, game, *, timeout_s, stop_event, progress):
        self.seen.append(game.slug)
        return GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=1,
            successful_spins=0,
            failed_spins=1,
            status="PARCIAL",
            symbol=game.symbol,
            run_dir="",
            error="HTTP 403 provider demo blocked",
        )

    def build_purchase_coverage(self, game, result):
        return {
            "state": PURCHASE_UNKNOWN,
            "provider": self.key,
            "game_slug": game.slug,
            "counts": {"total": 0, "complete": 0, "failed": 0, "unknown": 0},
            "options": [],
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

    def test_campaign_attaches_self_imposed_provider_rate_limit(self) -> None:
        provider = _FakeProvider()
        snapshot = attach_safety_limiter(provider, requests_per_minute=17)
        self.assertIsNotNone(provider.limiter)
        self.assertEqual(provider.limiter.requests_per_minute, 17)
        self.assertEqual(snapshot["requests_per_minute_limit"], 17)
        self.assertEqual(snapshot["window_seconds"], 60.0)

    def test_parallel_provider_opens_multiple_games_up_to_provider_cap(self) -> None:
        games = [
            Game(provider="fake", slug=f"g-{index}", name=f"G {index}", url=f"https://example/{index}")
            for index in range(6)
        ]
        provider = _ParallelProvider(cap=4)
        with tempfile.TemporaryDirectory() as temp:
            rows = run_selected_games(
                provider,
                games,
                timeout_s=5.0,
                stop_event=threading.Event(),
                output_dir=Path(temp),
                progress=lambda _message: None,
                concurrency=4,
            )
        self.assertEqual(len(rows), 6)
        self.assertGreaterEqual(provider.max_active, 2)
        self.assertLessEqual(provider.max_active, 4)

    def test_serial_provider_cap_overrides_requested_parallelism(self) -> None:
        games = [
            Game(provider="fake", slug=f"s-{index}", name=f"S {index}", url=f"https://example/{index}")
            for index in range(4)
        ]
        provider = _ParallelProvider(cap=1)
        with tempfile.TemporaryDirectory() as temp:
            rows = run_selected_games(
                provider,
                games,
                timeout_s=5.0,
                stop_event=threading.Event(),
                output_dir=Path(temp),
                progress=lambda _message: None,
                concurrency=8,
            )
        self.assertEqual(len(rows), 4)
        self.assertEqual(provider.max_active, 1)

    def test_inter_game_cooldown_prevents_back_to_back_game_bursts(self) -> None:
        games = [
            Game(provider="fake", slug="ok-1", name="OK 1", url="https://example/1"),
            Game(provider="fake", slug="ok-2", name="OK 2", url="https://example/2"),
        ]
        provider = _FakeProvider()
        event = _RecordingEvent()
        with tempfile.TemporaryDirectory() as temp:
            rows = run_selected_games(
                provider,
                games,
                timeout_s=5.0,
                stop_event=event,
                output_dir=Path(temp),
                progress=lambda _message: None,
                inter_game_delay_s=2.5,
                concurrency=1,
            )
        self.assertEqual(len(rows), 2)
        self.assertEqual(event.waits, [2.5])

    def test_repeated_identical_provider_failure_opens_circuit_without_more_network(self) -> None:
        games = [
            Game(provider="fake", slug=f"g-{index}", name=f"G {index}", url=f"https://example/{index}")
            for index in range(6)
        ]
        provider = _AlwaysFailProvider()
        with tempfile.TemporaryDirectory() as temp:
            rows = run_selected_games(
                provider,
                games,
                timeout_s=5.0,
                stop_event=threading.Event(),
                output_dir=Path(temp),
                progress=lambda _message: None,
                circuit_breaker_threshold=3,
                concurrency=1,
            )
        self.assertEqual(provider.seen, ["g-0", "g-1", "g-2"])
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row["coverage"]["state"] == PURCHASE_UNKNOWN for row in rows))
        self.assertIn("circuit breaker", rows[3]["runtime_error"].lower())
        self.assertIn("HTTP 403 provider demo blocked", rows[3]["runtime_error"])

    def test_repeated_partial_transport_block_also_opens_circuit(self) -> None:
        games = [
            Game(provider="fake", slug=f"p-{index}", name=f"P {index}", url=f"https://example/{index}")
            for index in range(5)
        ]
        provider = _PartialBlockedProvider()
        with tempfile.TemporaryDirectory() as temp:
            rows = run_selected_games(
                provider,
                games,
                timeout_s=5.0,
                stop_event=threading.Event(),
                output_dir=Path(temp),
                progress=lambda _message: None,
                circuit_breaker_threshold=3,
                concurrency=1,
            )
        self.assertEqual(provider.seen, ["p-0", "p-1", "p-2"])
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row["coverage"]["state"] == PURCHASE_UNKNOWN for row in rows))
        self.assertIn("circuit breaker", rows[3]["runtime_error"].lower())
        self.assertIn("HTTP 403 provider demo blocked", rows[3]["runtime_error"])

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
                concurrency=1,
            )
        self.assertEqual(provider.seen, ["ok-1", "boom", "ok-2"])
        self.assertEqual(provider.finalizer_calls, 0)
        self.assertEqual(provider.purchase_finalizer_calls, 2)
        self.assertEqual([row["coverage"]["state"] for row in rows], [
            PURCHASE_COMPLETE,
            PURCHASE_UNKNOWN,
            PURCHASE_COMPLETE,
        ])
        self.assertIn("synthetic launcher failure", rows[1]["runtime_error"])

    def test_incomplete_feature_session_blocks_purchase_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            provider = _FeatureGateProvider(root / "runtime")
            game = Game(
                provider="fake",
                slug="feature-open",
                name="Feature Open",
                url="https://example/feature-open",
            )
            rows = run_selected_games(
                provider,
                [game],
                timeout_s=5.0,
                stop_event=threading.Event(),
                output_dir=root / "out",
                progress=lambda _message: None,
                concurrency=1,
            )

        self.assertEqual(provider.purchase_finalizer_calls, 1)
        self.assertEqual(rows[0]["runtime_status"], "PARCIAL")
        self.assertEqual(rows[0]["coverage"]["state"], PURCHASE_UNKNOWN)
        self.assertEqual(rows[0]["coverage"]["counts"]["unknown"], 1)
        self.assertIn("feature session", rows[0]["coverage"]["options"][0]["reason"].lower())

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
                concurrency=1,
            )
        self.assertEqual(rows[0]["coverage"]["state"], PURCHASE_UNKNOWN)
        self.assertFalse(rows[0]["coverage"].get("no_purchase_proven", False))


if __name__ == "__main__":
    unittest.main()
