from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from scripts.provider_catalog_manifest import enumerate_provider_targets
from tester_spin.providers.base import ProviderAdapter
from tester_spin.storage import Storage

BatchProgress = Callable[[str, str], None]

DEFAULT_PROVIDER_KEYS = (
    "pragmatic",
    "1spin4win",
    "belatra",
    "rubyplay",
    "redtiger",
)


@dataclass(frozen=True, slots=True)
class CatalogRunResult:
    provider: str
    display_name: str
    status: str
    games: int
    authoritative: bool
    authority_reason: str
    reconciled: bool
    removed: int
    elapsed_s: float
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "display_name": self.display_name,
            "status": self.status,
            "games": self.games,
            "authoritative": self.authoritative,
            "authority_reason": self.authority_reason,
            "reconciled": self.reconciled,
            "removed": self.removed,
            "elapsed_s": round(self.elapsed_s, 3),
            "error": self.error,
        }


def _catalog_shrink_suspicious(
    previous_count: int,
    current_count: int,
    min_ratio: float = 0.60,
) -> bool:
    previous = max(0, int(previous_count))
    current = max(0, int(current_count))
    ratio = min(1.0, max(0.0, float(min_ratio)))
    return previous >= 100 and current < max(25, int(previous * ratio))


def build_providers(keys: Iterable[str], data_root: Path) -> list[ProviderAdapter]:
    """Instantiate only the requested provider adapters.

    BGaming is intentionally absent from this runner while that module remains
    outside the approved catalog batch set.
    """
    from tester_spin.providers import (
        BelatraProvider,
        OneSpin4WinProvider,
        PragmaticProvider,
        RedTigerProvider,
        RubyPlayProvider,
    )

    factories = {
        "pragmatic": PragmaticProvider,
        "1spin4win": OneSpin4WinProvider,
        "belatra": BelatraProvider,
        "rubyplay": RubyPlayProvider,
        "redtiger": RedTigerProvider,
    }
    providers: list[ProviderAdapter] = []
    seen: set[str] = set()
    for raw_key in keys:
        key = str(raw_key or "").strip().casefold()
        if not key or key in seen:
            continue
        try:
            factory = factories[key]
        except KeyError as exc:
            allowed = ", ".join(DEFAULT_PROVIDER_KEYS)
            raise ValueError(f"Proveedor no habilitado para batch: {key!r}. Válidos: {allowed}") from exc
        providers.append(factory(Path(data_root)))
        seen.add(key)
    return providers


def _run_one_provider(
    provider: ProviderAdapter,
    *,
    storage: Storage,
    requested_max_pages: int,
    full_catalog_requested: bool,
    progress: BatchProgress,
) -> CatalogRunResult:
    started = time.monotonic()
    stop_event = threading.Event()
    key = provider.key

    def emit(message: str) -> None:
        progress(key, str(message))

    try:
        previous_games = storage.list_games(key)
        previous_count = len(previous_games)
        manual_exclusions = storage.list_catalog_exclusions(key)

        invalid_reasons: dict[str, str] = {}
        for existing in previous_games:
            reason = provider.catalog_record_invalid_reason(existing)
            if reason:
                invalid_reasons[existing.slug] = reason
        if invalid_reasons:
            emit(
                "Saneamiento estructural diferido: "
                f"filas sospechosas={len(invalid_reasons)}."
            )

        provider.set_catalog_authority(True, "")
        emit(
            "Iniciando catálogo "
            + ("completo" if full_catalog_requested else f"limitado a {requested_max_pages} cargas")
            + " en modo low-traffic."
        )
        games = enumerate_provider_targets(
            provider,
            provider_key=key,
            requested_pages=requested_max_pages,
            stop_event=stop_event,
            progress=emit,
        )

        if manual_exclusions:
            blocked = {str(slug) for slug in manual_exclusions}
            before = len(games)
            games = [game for game in games if game.slug not in blocked]
            omitted = before - len(games)
            if omitted:
                emit(f"Exclusiones manuales aplicadas: {omitted} detecciones omitidas.")

        storage.upsert_games(games)

        authoritative = bool(getattr(provider, "catalog_crawl_authoritative", True))
        authority_reason = str(getattr(provider, "catalog_crawl_reason", "") or "")
        min_ratio = float(getattr(provider, "min_catalog_reconcile_ratio", 0.60) or 0.60)
        shrink_suspicious = _catalog_shrink_suspicious(
            previous_count,
            len(games),
            min_ratio,
        )

        can_reconcile = (
            full_catalog_requested
            and not stop_event.is_set()
            and bool(games)
            and authoritative
            and not shrink_suspicious
        )
        reconciled = False
        removed = 0
        if can_reconcile:
            removed = storage.reconcile_provider_games(
                key,
                {game.slug for game in games},
            )
            reconciled = True
            emit(
                f"Reconciliación completa: actuales={len(games)}, "
                f"obsoletos eliminados={removed}."
            )

        partial_reasons: list[str] = []
        if not games:
            partial_reasons.append("crawl vacío")
        if not authoritative:
            partial_reasons.append(authority_reason or "crawler/fuente no autoritativa")
        if shrink_suspicious:
            partial_reasons.append(
                f"reducción anómala {previous_count}→{len(games)} "
                f"(mínimo seguro={min_ratio:.0%})"
            )
        if stop_event.is_set():
            partial_reasons.append("detención solicitada")

        status = "PARCIAL" if partial_reasons else "OK"
        if full_catalog_requested and not can_reconcile and status == "OK":
            status = "PARCIAL"
            partial_reasons.append("crawl completo sin reconciliación segura")

        if partial_reasons:
            emit(
                "RECONCILIACIÓN BLOQUEADA: no se eliminará ningún juego "
                f"({'; '.join(partial_reasons)})."
            )

        return CatalogRunResult(
            provider=key,
            display_name=provider.display_name,
            status=status,
            games=len(games),
            authoritative=authoritative,
            authority_reason=authority_reason,
            reconciled=reconciled,
            removed=removed,
            elapsed_s=time.monotonic() - started,
            error="; ".join(partial_reasons) if status == "PARCIAL" else "",
        )
    except Exception as exc:
        emit(f"ERROR: {type(exc).__name__}: {exc}")
        return CatalogRunResult(
            provider=key,
            display_name=provider.display_name,
            status="ERROR",
            games=0,
            authoritative=False,
            authority_reason=str(getattr(provider, "catalog_crawl_reason", "") or ""),
            reconciled=False,
            removed=0,
            elapsed_s=time.monotonic() - started,
            error=f"{type(exc).__name__}: {exc}",
        )


def run_provider_catalogs(
    providers: Iterable[ProviderAdapter],
    *,
    storage: Storage,
    workers: int,
    max_pages: int,
    progress: BatchProgress,
) -> list[CatalogRunResult]:
    """Crawl provider catalogs concurrently while keeping each adapter isolated."""
    provider_list = list(providers)
    if not provider_list:
        return []

    requested_max_pages = int(max_pages)
    if requested_max_pages < 0:
        raise ValueError("max_pages debe ser 0 o un entero positivo")
    full_catalog_requested = requested_max_pages == 0
    max_workers = min(len(provider_list), max(1, int(workers)))

    future_to_provider: dict[Future[CatalogRunResult], ProviderAdapter] = {}
    results_by_key: dict[str, CatalogRunResult] = {}
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="provider-catalog",
    ) as pool:
        for provider in provider_list:
            future = pool.submit(
                _run_one_provider,
                provider,
                storage=storage,
                requested_max_pages=requested_max_pages,
                full_catalog_requested=full_catalog_requested,
                progress=progress,
            )
            future_to_provider[future] = provider

        for future in as_completed(future_to_provider):
            provider = future_to_provider[future]
            try:
                result = future.result()
            except Exception as exc:  # defensive: _run_one_provider already contains failures
                result = CatalogRunResult(
                    provider=provider.key,
                    display_name=provider.display_name,
                    status="ERROR",
                    games=0,
                    authoritative=False,
                    authority_reason="",
                    reconciled=False,
                    removed=0,
                    elapsed_s=0.0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            results_by_key[provider.key] = result

    return [results_by_key[provider.key] for provider in provider_list]