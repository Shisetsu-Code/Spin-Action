from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from scripts.purchase_campaign import run_selected_games
from tester_spin.models import Game, GameTestResult
from tester_spin.purchase_coverage import PURCHASE_UNKNOWN


class _BackoffProvider:
    key = "fake"
    max_test_concurrency = 1

    def __init__(self) -> None:
        self.current_rpm = 2000
        self.adjustments: list[int] = []

    def effective_test_concurrency(self, requested: int) -> int:
        return 1

    def set_provider_request_rate_limit(self, requests_per_minute: int) -> int:
        self.current_rpm = int(requests_per_minute)
        self.adjustments.append(self.current_rpm)
        return self.current_rpm

    def provider_request_rate_snapshot(self):
        return {"requests_per_minute_limit": self.current_rpm}

    def test_purchase_paths(self, game, *, timeout_s, stop_event, progress):
        return GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=1,
            successful_spins=0,
            failed_spins=1,
            status="PARCIAL",
            error="HTTP 429 Too Many Requests",
        )

    def build_purchase_coverage(self, game, result):
        return {
            "state": PURCHASE_UNKNOWN,
            "provider": self.key,
            "game_slug": game.slug,
            "counts": {"total": 0, "complete": 0, "failed": 0, "unknown": 0},
            "options": [],
        }


def test_transport_pressure_halves_shared_rate_down_to_floor() -> None:
    provider = _BackoffProvider()
    games = [
        Game(provider="fake", slug=f"g-{i}", name=f"G {i}", url=f"https://example/{i}")
        for i in range(4)
    ]
    with tempfile.TemporaryDirectory() as temp:
        rows = run_selected_games(
            provider,
            games,
            timeout_s=5.0,
            stop_event=threading.Event(),
            output_dir=Path(temp),
            progress=lambda _message: None,
            concurrency=1,
            circuit_breaker_threshold=0,
            min_requests_per_minute=250,
            rate_backoff_factor=0.5,
        )

    assert len(rows) == 4
    assert provider.adjustments == [1000, 500, 250]
    assert provider.current_rpm == 250
