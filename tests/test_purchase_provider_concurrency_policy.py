from tester_spin.providers.belatra_farm_adapter import BelatraProvider
from tester_spin.providers.one_spin4win_farm_adapter import OneSpin4WinProvider
from tester_spin.providers.pragmatic_farm_adapter import PragmaticProvider
from tester_spin.providers.redtiger.farm_adapter import RedTigerProvider
from tester_spin.providers.rubyplay.farm_adapter import RubyPlayProvider


def test_purchase_provider_concurrency_caps_are_explicit() -> None:
    assert PragmaticProvider.max_test_concurrency == 4
    assert OneSpin4WinProvider.max_test_concurrency == 4
    assert BelatraProvider.max_test_concurrency == 1
    assert RubyPlayProvider.max_test_concurrency == 1
    assert RedTigerProvider.max_test_concurrency == 1
