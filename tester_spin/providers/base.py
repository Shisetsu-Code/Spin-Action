from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable

from tester_spin.models import Game, GameTestResult

Progress = Callable[[str], None]


class ProviderAdapter(ABC):
    key: str
    display_name: str
    catalog_url: str

    @abstractmethod
    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
    ) -> list[Game]:
        raise NotImplementedError

    @abstractmethod
    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        raise NotImplementedError


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderAdapter] = {}

    def register(self, provider: ProviderAdapter) -> None:
        if provider.key in self._providers:
            raise ValueError(f"Proveedor duplicado: {provider.key}")
        self._providers[provider.key] = provider

    def get(self, key: str) -> ProviderAdapter:
        try:
            return self._providers[key]
        except KeyError as exc:
            raise KeyError(f"Proveedor no registrado: {key}") from exc

    def all(self) -> list[ProviderAdapter]:
        return list(self._providers.values())
