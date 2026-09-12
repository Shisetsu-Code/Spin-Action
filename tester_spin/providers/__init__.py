from tester_spin.providers.base import ProviderAdapter, ProviderRegistry
from tester_spin.providers.bgaming_exhaustive import BGamingProvider
from tester_spin.providers.belatra_exhaustive import BelatraProvider
from tester_spin.providers.one_spin4win_exhaustive import OneSpin4WinProvider
from tester_spin.providers.pragmatic_hybrid import PragmaticProvider
from tester_spin.providers.redtiger import RedTigerProvider
from tester_spin.providers.rubyplay.exhaustive import RubyPlayProvider

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
