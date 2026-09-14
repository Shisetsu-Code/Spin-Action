from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from tester_spin.models import Game


Progress = Callable[[str], None]


@contextlib.contextmanager
def _suppress_catalog_asset_downloads(provider: Any):
    """Temporarily disable catalog-only asset downloads on a lab provider instance.

    The production crawlers remain unchanged. This only prevents thumbnail/image
    traffic while Actions is enumerating targets for validation.
    """
    saved: dict[str, Any] = {}
    for name in ("_download_thumbnail", "_persist_thumbnail"):
        if not hasattr(provider, name):
            continue
        saved[name] = getattr(provider, name)
        setattr(provider, name, lambda *args, **kwargs: None)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(provider, name, value)


def _validate_targets(provider: Any, games: Iterable[Game]) -> list[Game]:
    ordered = sorted(
        list(games),
        key=lambda game: (game.slug.casefold(), game.name.casefold(), game.url),
    )
    seen: set[str] = set()
    for game in ordered:
        slug = str(game.slug or "").strip()
        if not slug:
            raise RuntimeError(f"{getattr(provider, 'key', 'provider')}: catálogo contiene slug vacío")
        if slug in seen:
            raise RuntimeError(
                f"{getattr(provider, 'key', 'provider')}: slug duplicado en catálogo: {slug}"
            )
        seen.add(slug)
        validator = getattr(provider, "catalog_record_invalid_reason", None)
        if callable(validator):
            reason = str(validator(game) or "").strip()
            if reason:
                raise RuntimeError(
                    f"{getattr(provider, 'key', 'provider')}: target inválido {slug}: {reason}"
                )
    return ordered


def enumerate_provider_targets(
    provider: Any,
    *,
    provider_key: str,
    requested_pages: int,
    stop_event: threading.Event,
    progress: Progress,
) -> list[Game]:
    """Enumerate validation targets with catalog asset traffic suppressed.

    ``requested_pages=0`` means full catalog. Pragmatic uses its dedicated
    sequential lab enumerator so it does not enter the production artifact
    persistence stage at all. Other providers reuse their own catalog parser
    with only thumbnail/image download hooks disabled on this lab instance.
    """
    key = str(provider_key or getattr(provider, "key", "")).strip().lower()
    pages = int(requested_pages)
    if pages < 0:
        raise ValueError("requested_pages debe ser 0 o positivo")

    if key == "pragmatic":
        from scripts.pragmatic_catalog_targets import enumerate_pragmatic_targets

        games = enumerate_pragmatic_targets(
            provider,
            stop_event=stop_event,
            progress=progress,
            max_pages=pages,
        )
        return _validate_targets(provider, games)

    crawl_pages = 10_000 if pages == 0 else max(1, pages)
    with _suppress_catalog_asset_downloads(provider):
        games = provider.crawl_catalog(
            stop_event=stop_event,
            progress=progress,
            max_pages=crawl_pages,
            on_game=None,
        )
    return _validate_targets(provider, games)


def remaining_targets(
    games: Iterable[Game],
    previous_verdicts: Mapping[str, str] | None,
) -> list[Game]:
    """Return every target that does not already have a proven COMPLETE verdict."""
    verdicts = previous_verdicts or {}
    return [
        game
        for game in games
        if str(verdicts.get(game.slug) or "").strip().upper() != "COMPLETE"
    ]


def build_validation_shards(
    games: Iterable[Game],
    previous_verdicts: Mapping[str, str] | None,
    *,
    shard_size: int,
) -> list[list[Game]]:
    """Split unresolved targets deterministically, exactly once per slug."""
    size = int(shard_size)
    if size < 1:
        raise ValueError("shard_size debe ser >= 1")

    pending = sorted(
        remaining_targets(games, previous_verdicts),
        key=lambda game: (game.slug.casefold(), game.name.casefold(), game.url),
    )
    return [pending[index:index + size] for index in range(0, len(pending), size)]


__all__ = [
    "build_validation_shards",
    "enumerate_provider_targets",
    "remaining_targets",
]
