from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable

from tester_spin.models import Game, GameTestResult

Progress = Callable[[str], None]
GameCallback = Callable[[Game], None]


class ProviderAdapter(ABC):
    key: str
    display_name: str
    catalog_url: str
    # Optional provider-side cap. Some public/demo backends invalidate or reject
    # concurrent sessions even when Tester-Spin can technically run more workers.
    max_test_concurrency: int | None = None

    def effective_test_concurrency(self, requested: int) -> int:
        value = max(1, int(requested))
        cap = self.max_test_concurrency
        if cap is None:
            return value
        return min(value, max(1, int(cap)))

    @abstractmethod
    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
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
