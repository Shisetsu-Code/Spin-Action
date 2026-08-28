from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import ProviderAdapter
from tester_spin.scheduler import run_game_tests

Progress = Callable[[str], None]
ResultCallback = Callable[[GameTestResult], None]


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    concurrency: int
    spins_per_game: int
    delay_between_starts_s: float
    timeout_s: float


class ExecutionBackend(ABC):
    """Transport-neutral execution boundary used by the GUI.

    A backend owns where game tests run. The GUI only submits a batch and receives
    progress/results. A future remote backend can therefore replace the local
    implementation without changing Tk, the provider state machine, or persistence.
    """

    key: str
    display_name: str

    @abstractmethod
    def run(
        self,
        provider: ProviderAdapter,
        games: Iterable[Game],
        *,
        config: ExecutionConfig,
        stop_event: threading.Event,
        progress: Progress,
        on_result: ResultCallback,
    ) -> None:
        raise NotImplementedError


class LocalThreadExecutionBackend(ExecutionBackend):
    """Bounded local worker pool; network-bound games execute concurrently."""

    key = "local-threaded"
    display_name = "Local multithread"

    def run(
        self,
        provider: ProviderAdapter,
        games: Iterable[Game],
        *,
        config: ExecutionConfig,
        stop_event: threading.Event,
        progress: Progress,
        on_result: ResultCallback,
    ) -> None:
        run_game_tests(
            provider,
            games,
            concurrency=config.concurrency,
            spins_per_game=config.spins_per_game,
            delay_between_starts_s=config.delay_between_starts_s,
            timeout_s=config.timeout_s,
            stop_event=stop_event,
            progress=progress,
            on_result=on_result,
        )
