from __future__ import annotations

import threading
import unittest
from unittest import mock

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import ProviderAdapter
from tester_spin.providers.belatra import BelatraProvider as BaseBelatraProvider
from tester_spin.providers.belatra_exhaustive import BelatraProvider as ExhaustiveBelatraProvider
from tester_spin.providers.belatra_farm_adapter import BelatraProvider
from tester_spin.providers.bgaming.execution import BGamingExecutionMixin
from tester_spin.providers.bgaming_farm_adapter import BGamingProvider
from tester_spin.providers.one_spin4win_farm_adapter import OneSpin4WinProvider
from tester_spin.providers.pragmatic import PragmaticProvider as CorePragmaticProvider
from tester_spin.providers.pragmatic_farm_adapter import PragmaticProvider
from tester_spin.providers.redtiger.execution import RedTigerExecutionMixin
from tester_spin.providers.redtiger.farm_adapter import RedTigerProvider
from tester_spin.providers.rubyplay.execution import RubyPlayExecutionMixin
from tester_spin.providers.rubyplay.farm_adapter import RubyPlayProvider


def _game(provider: str) -> Game:
    return Game(provider=provider, slug="demo", name="Demo", url="https://example.invalid/demo")


def _result(provider: str) -> GameTestResult:
    return GameTestResult(
        provider=provider,
        slug="demo",
        game_name="Demo",
        game_url="https://example.invalid/demo",
        requested_spins=3,
        successful_spins=3,
        failed_spins=0,
        status="OK",
    )


class _UnsupportedProvider(ProviderAdapter):
    key = "unsupported"
    display_name = "Unsupported"
    catalog_url = "https://example.invalid"

    def crawl_catalog(self, *, stop_event, progress, max_pages=100, on_game=None):
        return []

    def test_game(self, game, *, spins, timeout_s, stop_event, progress):
        return _result(self.key)


class NaturalSpinContractTests(unittest.TestCase):
    def test_base_provider_fails_closed_without_natural_spin_implementation(self) -> None:
        provider = _UnsupportedProvider()
        with self.assertRaises(NotImplementedError):
            provider.test_natural_spins(
                _game(provider.key),
                spins=3000,
                timeout_s=20.0,
                stop_event=threading.Event(),
                progress=lambda _message: None,
            )

    def test_one_spin4win_natural_soak_reuses_spin_only_executor(self) -> None:
        provider = object.__new__(OneSpin4WinProvider)
        expected = _result("1spin4win")
        stop = threading.Event()
        progress = lambda _message: None
        game = _game("1spin4win")

        with mock.patch.object(OneSpin4WinProvider, "test_game", return_value=expected) as execute:
            result = provider.test_natural_spins(
                game,
                spins=3000,
                timeout_s=20.0,
                stop_event=stop,
                progress=progress,
            )

        self.assertIs(result, expected)
        execute.assert_called_once_with(
            game,
            spins=3000,
            timeout_s=20.0,
            stop_event=stop,
            progress=progress,
        )

    def test_belatra_natural_soak_bypasses_exhaustive_selector_expansion(self) -> None:
        provider = object.__new__(BelatraProvider)
        expected = _result("belatra")
        stop = threading.Event()
        progress = lambda _message: None
        game = _game("belatra")

        with (
            mock.patch.object(BaseBelatraProvider, "test_game", return_value=expected) as base_execute,
            mock.patch.object(
                ExhaustiveBelatraProvider,
                "test_game",
                side_effect=AssertionError("exhaustive path must not run"),
            ),
        ):
            result = provider.test_natural_spins(
                game,
                spins=3000,
                timeout_s=20.0,
                stop_event=stop,
                progress=progress,
            )

        self.assertIs(result, expected)
        base_execute.assert_called_once_with(
            provider,
            game,
            spins=3000,
            timeout_s=20.0,
            stop_event=stop,
            progress=progress,
        )

    def test_pragmatic_natural_soak_calls_core_executor_with_natural_only(self) -> None:
        provider = object.__new__(PragmaticProvider)
        expected = _result("pragmatic")
        stop = threading.Event()
        progress = lambda _message: None
        game = _game("pragmatic")
        with mock.patch.object(CorePragmaticProvider, "test_game", return_value=expected) as execute:
            try:
                result = provider.test_natural_spins(
                    game, spins=3000, timeout_s=20.0, stop_event=stop, progress=progress
                )
            except NotImplementedError:
                self.fail("PragmaticProvider must implement test_natural_spins")
        self.assertIs(result, expected)
        execute.assert_called_once_with(
            provider,
            game,
            spins=3000,
            timeout_s=20.0,
            stop_event=stop,
            progress=progress,
            natural_only=True,
        )

    def test_rubyplay_natural_soak_calls_execution_mixin_with_natural_only(self) -> None:
        provider = object.__new__(RubyPlayProvider)
        expected = _result("rubyplay")
        stop = threading.Event()
        progress = lambda _message: None
        game = _game("rubyplay")
        with mock.patch.object(RubyPlayExecutionMixin, "test_game", return_value=expected) as execute:
            try:
                result = provider.test_natural_spins(
                    game, spins=3000, timeout_s=20.0, stop_event=stop, progress=progress
                )
            except NotImplementedError:
                self.fail("RubyPlayProvider must implement test_natural_spins")
        self.assertIs(result, expected)
        execute.assert_called_once_with(
            provider,
            game,
            spins=3000,
            timeout_s=20.0,
            stop_event=stop,
            progress=progress,
            natural_only=True,
        )

    def test_redtiger_natural_soak_calls_execution_mixin_with_natural_only(self) -> None:
        provider = object.__new__(RedTigerProvider)
        expected = _result("redtiger")
        stop = threading.Event()
        progress = lambda _message: None
        game = _game("redtiger")
        with mock.patch.object(RedTigerExecutionMixin, "test_game", return_value=expected) as execute:
            try:
                result = provider.test_natural_spins(
                    game, spins=3000, timeout_s=20.0, stop_event=stop, progress=progress
                )
            except NotImplementedError:
                self.fail("RedTigerProvider must implement test_natural_spins")
        self.assertIs(result, expected)
        self.assertTrue(execute.called)
        self.assertTrue(execute.call_args.kwargs["natural_only"])

    def test_bgaming_natural_soak_calls_execution_mixin_with_natural_only(self) -> None:
        provider = object.__new__(BGamingProvider)
        expected = _result("bgaming")
        stop = threading.Event()
        progress = lambda _message: None
        game = _game("bgaming")
        with mock.patch.object(BGamingExecutionMixin, "test_game", return_value=expected) as execute:
            try:
                result = provider.test_natural_spins(
                    game, spins=3000, timeout_s=20.0, stop_event=stop, progress=progress
                )
            except NotImplementedError:
                self.fail("BGamingProvider must implement test_natural_spins")
        self.assertIs(result, expected)
        execute.assert_called_once_with(
            provider,
            game,
            spins=3000,
            timeout_s=20.0,
            stop_event=stop,
            progress=progress,
            natural_only=True,
        )


if __name__ == "__main__":
    unittest.main()
