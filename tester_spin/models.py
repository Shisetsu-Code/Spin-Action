from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class Game:
    provider: str
    slug: str
    name: str
    url: str
    thumbnail_url: str = ""
    thumbnail_path: str = ""
    symbol: str = ""
    discovered_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    last_status: str = "PENDIENTE"
    last_error: str = ""
    last_test_at: str = ""
    last_latency_ms: float | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.provider, self.slug


@dataclass(slots=True)
class SpinAttempt:
    number: int
    ok: bool
    status_code: int | None = None
    elapsed_ms: float | None = None
    symbol: str = ""
    endpoint: str = ""
    na: str = ""
    error: str = ""
    bootstrap: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "ok": self.ok,
            "status_code": self.status_code,
            "elapsed_ms": self.elapsed_ms,
            "symbol": self.symbol,
            "endpoint": self.endpoint,
            "na": self.na,
            "error": self.error,
            "bootstrap": self.bootstrap,
        }


@dataclass(slots=True)
class GameTestResult:
    provider: str
    slug: str
    game_name: str
    game_url: str
    requested_spins: int
    successful_spins: int
    failed_spins: int
    status: str
    symbol: str = ""
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = field(default_factory=utc_now_iso)
    elapsed_ms: float = 0.0
    error: str = ""
    attempts: list[SpinAttempt] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "slug": self.slug,
            "game_name": self.game_name,
            "game_url": self.game_url,
            "requested_spins": self.requested_spins,
            "successful_spins": self.successful_spins,
            "failed_spins": self.failed_spins,
            "status": self.status,
            "symbol": self.symbol,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }
