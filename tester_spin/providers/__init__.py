from tester_spin.providers.base import ProviderAdapter, ProviderRegistry
from tester_spin.providers.bgaming_farm_adapter import BGamingProvider
from tester_spin.providers.belatra_farm_adapter import BelatraProvider
from tester_spin.providers.one_spin4win_farm_adapter import OneSpin4WinProvider
from tester_spin.providers.pragmatic_farm_adapter import PragmaticProvider
from tester_spin.providers.redtiger.farm_adapter import RedTigerProvider
from tester_spin.providers.rubyplay.farm_adapter import RubyPlayProvider

__all__ = [
    "ProviderAdapter",
    "ProviderRegistry",
    "PragmaticProvider",
    "OneSpin4WinProvider",
    "BelatraProvider",
    "BGamingProvider",
    "RubyPlayProvider",
    "RedTigerProvider",
]
