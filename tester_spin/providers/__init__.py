from tester_spin.providers.base import ProviderAdapter, ProviderRegistry
from tester_spin.providers.belatra import BelatraProvider
from tester_spin.providers.one_spin4win import OneSpin4WinProvider
from tester_spin.providers.pragmatic_hybrid import PragmaticProvider

__all__ = [
    "ProviderAdapter",
    "ProviderRegistry",
    "PragmaticProvider",
    "OneSpin4WinProvider",
    "BelatraProvider",
]
