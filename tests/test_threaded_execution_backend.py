from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import requests

from tester_spin.execution_backend import ExecutionConfig, LocalThreadExecutionBackend
from tester_spin.providers.pragmatic_symbol_resolver import _resolver_session


class _DummyProvider:
    def __init__(self) -> None:
        self.http = requests.Session()
        self.http.headers.update({"X-Test": "thread-local"})
        self.http.cookies.set("seed", "cookie")


class ThreadedExecutionBackendTests(unittest.TestCase):
    def test_local_backend_maps_config_to_scheduler(self) -> None:
        backend = LocalThreadExecutionBackend()
        config = ExecutionConfig(
            concurrency=7,
            spins_per_game=3,
            delay_between_starts_s=0.25,
            timeout_s=19.0,
        )
        provider = object()
        games = [object(), object()]
        stop_event = threading.Event()
        progress = lambda _message: None
        on_result = lambda _result: None

        with patch("tester_spin.execution_backend.run_game_tests") as run:
            backend.run(
                provider,  # type: ignore[arg-type]
                games,  # type: ignore[arg-type]
                config=config,
                stop_event=stop_event,
                progress=progress,
                on_result=on_result,
            )

        run.assert_called_once_with(
            provider,
            games,
            concurrency=7,
            spins_per_game=3,
            delay_between_starts_s=0.25,
            timeout_s=19.0,
            stop_event=stop_event,
            progress=progress,
            on_result=on_result,
        )

    def test_symbol_resolver_session_is_stable_inside_one_thread(self) -> None:
        provider = _DummyProvider()
        first = _resolver_session(provider)
        second = _resolver_session(provider)
        self.assertIs(first, second)
        self.assertEqual(first.headers.get("X-Test"), "thread-local")
        self.assertEqual(first.cookies.get("seed"), "cookie")

    def test_symbol_resolver_sessions_are_isolated_between_threads(self) -> None:
        provider = _DummyProvider()
        barrier = threading.Barrier(2)

        def get_session_id() -> int:
            session = _resolver_session(provider)
            barrier.wait(timeout=5)
            return id(session)

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _n: get_session_id(), range(2)))

        self.assertEqual(len(set(ids)), 2)


if __name__ == "__main__":
    unittest.main()
