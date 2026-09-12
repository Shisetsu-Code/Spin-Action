from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

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
    # Catalog crawlers may explicitly downgrade a run to non-authoritative when
    # they use a degraded/fallback source. Non-authoritative runs can add/update
    # validated rows but must never delete existing catalog rows.
    catalog_crawl_authoritative: bool = True
    catalog_crawl_reason: str = ""
    # Reconciliation safety threshold. A provider may tighten this when its
    # catalogue is large/stable and temporary WAF/parser failures are more likely
    # than large legitimate removals.
    min_catalog_reconcile_ratio: float = 0.60

    def set_catalog_authority(self, authoritative: bool, reason: str = "") -> None:
        self.catalog_crawl_authoritative = bool(authoritative)
        self.catalog_crawl_reason = str(reason or "")

    def catalog_record_invalid_reason(self, game: Game) -> str:
        """Return a reason only for records that are provably malformed.

        This hook is intentionally conservative. It is not a replacement for
        provider reconciliation and must never be used to infer that a merely
        old/unreachable game has been removed from the provider.
        """
        return ""

    def effective_test_concurrency(self, requested: int) -> int:
        value = max(1, int(requested))
        cap = self.max_test_concurrency
        if cap is None:
            return value
        return min(value, max(1, int(cap)))

    def prepare_test_artifacts(
        self,
        game: Game,
        *,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> None:
        """Best-effort pre-test artifact preparation.

        Providers may override this to capture reusable diagnostics such as HARs.
        The default is intentionally a no-op so provider implementations remain
        autonomous.
        """
        return None

    def har_artifact_dir(self, game: Game) -> Path | None:
        """Return the folder containing this game's HAR/diagnostics, if any.

        The GUI uses this hook instead of knowing provider-specific storage layouts.
        Providers that do not maintain HAR artifacts keep the default no-op.
        """
        return None

    def finalize_test_result(
        self,
        result: GameTestResult,
        *,
        progress: Progress,
    ) -> GameTestResult:
        """Apply provider-neutral completeness gates after the adapter finishes.

        An adapter owns the wire protocol and is the only layer allowed to execute
        provider-specific continuations. The neutral finalizer verifies that any
        selectable branch exposed by adapter metadata or persisted JSON evidence was
        actually covered. Unknown wire contracts therefore stay PARCIAL instead of
        being silently reported as OK.
        """
        from tester_spin.providers.path_coverage import enforce_complete_path_coverage

        return enforce_complete_path_coverage(result, progress=progress)

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
