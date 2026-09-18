from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult

Progress = Callable[[str], None]
GameCallback = Callable[[Game], None]


class ProviderAdapter(ABC):
    def __init__(self, data_root: Path | None = None) -> None:
        self.data_root = Path(data_root) if data_root is not None else Path(".")

    key: str
    display_name: str
    catalog_url: str
    # Optional provider-side cap. Some public/demo backends invalidate or reject
    # concurrent sessions even when Tester-Spin can technically run more workers.
    max_test_concurrency: int | None = None
    catalog_crawl_authoritative: bool = True
    catalog_crawl_reason: str = ""
    min_catalog_reconcile_ratio: float = 0.60

    def set_catalog_authority(self, authoritative: bool, reason: str = "") -> None:
        self.catalog_crawl_authoritative = bool(authoritative)
        self.catalog_crawl_reason = str(reason or "")

    def set_request_rate_limiter(self, limiter) -> None:
        """Attach one limiter shared by every game worker for this provider instance."""
        self._provider_request_rate_limiter = limiter

    def set_provider_request_rate_limit(self, requests_per_minute: int) -> int:
        """Adjust the attached shared limiter in place.

        Active workers keep the same limiter object, so a campaign can reduce the
        provider-wide ceiling without creating independent per-worker budgets.
        """
        limiter = getattr(self, "_provider_request_rate_limiter", None)
        if limiter is None:
            raise RuntimeError("provider request rate limiter is not attached")
        setter = getattr(limiter, "set_requests_per_minute", None)
        if not callable(setter):
            raise TypeError("attached provider request rate limiter is not adjustable")
        return int(setter(int(requests_per_minute)))

    def acquire_provider_request_slot(
        self,
        *,
        stop_event: threading.Event | None = None,
    ) -> bool:
        """Reserve one outbound provider-protocol request when a limiter is attached."""
        limiter = getattr(self, "_provider_request_rate_limiter", None)
        if limiter is None:
            return True
        return bool(limiter.acquire(stop_event=stop_event))

    def provider_request_rate_snapshot(self) -> dict[str, Any]:
        limiter = getattr(self, "_provider_request_rate_limiter", None)
        if limiter is None:
            return {}
        snapshot = limiter.snapshot()
        return dict(snapshot) if isinstance(snapshot, dict) else {}

    def catalog_record_invalid_reason(self, game: Game) -> str:
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
        return None

    def har_artifact_dir(self, game: Game) -> Path | None:
        return None

    def farm_contract_dir(self, game: Game) -> Path | None:
        return None

    def build_farm_contract(
        self,
        game: Game,
        result: GameTestResult,
    ) -> dict[str, Any]:
        from tester_spin.farm_contract import SCHEMA

        return {
            "schema": SCHEMA,
            "provider": result.provider,
            "game": {
                "slug": result.slug,
                "name": result.game_name,
                "symbol": result.symbol,
            },
            "ready": False,
            "source": {
                "run": result.finished_at,
                "protocol_family": "unsupported",
                "result_status": result.status,
            },
            "bootstrap": {},
            "modes": [],
            "continuations": {"known": [], "unresolved": []},
            "terminal_contract": {},
            "protocol": {},
            "unresolved": ["PROVIDER_CONTRACT_UNSUPPORTED"],
        }

    def validate_farm_contract(self, contract: dict[str, Any]) -> list[str]:
        return []

    def build_feature_sessions(self, result: GameTestResult) -> dict[str, Any]:
        """Return provider-normalized feature-session evidence.

        Provider adapters override this hook. The neutral default emits no
        sessions and therefore never invents a feature lifecycle for an
        unsupported provider.
        """
        from tester_spin.feature_sessions import finalize_feature_session_report

        return finalize_feature_session_report(
            result,
            sessions=[],
            authority=f"{self.key}:feature-session-normalization-unsupported",
        )

    def _finalize_feature_and_path_coverage(
        self,
        result: GameTestResult,
        *,
        progress: Progress,
    ) -> GameTestResult:
        from tester_spin.feature_sessions import enforce_complete_feature_sessions
        from tester_spin.providers.path_coverage import enforce_complete_path_coverage

        report = self.build_feature_sessions(result)
        result = enforce_complete_feature_sessions(result, report, progress=progress)
        return enforce_complete_path_coverage(result, progress=progress)

    def finalize_test_result(
        self,
        result: GameTestResult,
        *,
        progress: Progress,
    ) -> GameTestResult:
        from tester_spin.sample_catalog import write_sample_catalog

        try:
            samples = write_sample_catalog(result, samples_per_path=result.samples_per_path)
            if result.run_dir and result.status == "OK" and not samples["observed_paths_sampled"]:
                result.status = "PARCIAL"
                message = "Muestreo pendiente: faltan rondas válidas o evidencia para las rutas observadas; ver sample-catalog.json."
                result.error = (result.error + " " + message).strip()
                progress(message)
        except OSError as exc:
            message = f"No se pudo guardar el catálogo de muestras: {exc}"
            if result.status == "OK":
                result.status = "PARCIAL"
                result.error = (result.error + " " + message).strip()
            progress(message)
        if self is None:
            return result
        return self._finalize_feature_and_path_coverage(result, progress=progress)

    def finalize_purchase_result(
        self,
        result: GameTestResult,
        *,
        progress: Progress,
    ) -> GameTestResult:
        """Finalize purchase evidence without natural-spin sampling.

        Purchase campaigns still require normalized feature sessions and complete
        provider branch coverage before a root purchase can be promoted.
        """
        return self._finalize_feature_and_path_coverage(result, progress=progress)

    def test_natural_spins(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        """Run only the natural/base-spin entry path for soak validation.

        Providers must opt in explicitly. The default is fail-closed because
        delegating to ``test_game`` could multiply purchases, ante bets, selector
        matrices or other explicit coverage paths by the natural-spin budget.
        """
        raise NotImplementedError(
            f"{self.key}: natural-spin-only execution is not implemented"
        )

    def test_purchase_paths(
        self,
        game: Game,
        *,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        """Run the minimum provider-native execution needed for purchase coverage.

        The default reuses one normal exhaustive provider iteration. Providers may
        override this when their ordinary test expands unrelated branch matrices.
        Purchase semantics still remain provider-local and are evaluated later by
        ``build_purchase_coverage``.
        """
        return self.test_game(
            game,
            spins=1,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )

    def build_purchase_coverage(
        self,
        game: Game,
        result: GameTestResult,
    ) -> dict[str, Any]:
        """Return a fail-closed purchase coverage envelope.

        Providers must override this method to promote any purchase or authoritative
        no-purchase result. The base implementation deliberately cannot infer either.
        """
        from tester_spin.purchase_coverage import finalize_purchase_coverage

        return finalize_purchase_coverage(
            result,
            options=[],
            inventory_state="UNKNOWN",
            authority=f"{self.key}:purchase-coverage-unsupported",
            no_purchase_proven=False,
            reason="Provider has no purchase coverage adapter; presence and absence remain unresolved.",
        )

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
        self._providers: dict[str, type[ProviderAdapter]] = {}

    def register(self, provider_cls: type[ProviderAdapter]) -> None:
        self._providers[provider_cls.key] = provider_cls

    def create(self, key: str, data_root: Path) -> ProviderAdapter:
        provider_cls = self._providers[key]
        return provider_cls(data_root)

    def keys(self) -> list[str]:
        return sorted(self._providers)
