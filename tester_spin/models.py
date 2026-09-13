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
    mode_id: str = "SPIN"
    mode_kind: str = "SPIN"
    provider_bl: int | None = None
    provider_pur: int | None = None
    status_code: int | None = None
    elapsed_ms: float | None = None
    symbol: str = ""
    endpoint: str = ""
    na: str = ""
    terminal: bool = False
    wire_steps: int = 0
    warning: str = ""
    error: str = ""
    artifact_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "ok": self.ok,
            "mode_id": self.mode_id,
            "mode_kind": self.mode_kind,
            "provider_bl": self.provider_bl,
            "provider_pur": self.provider_pur,
            "status_code": self.status_code,
            "elapsed_ms": self.elapsed_ms,
            "symbol": self.symbol,
            "endpoint": self.endpoint,
            "na": self.na,
            "terminal": self.terminal,
            "wire_steps": self.wire_steps,
            "warning": self.warning,
            "error": self.error,
            "artifact_dir": self.artifact_dir,
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
    discovered_modes: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = field(default_factory=utc_now_iso)
    elapsed_ms: float = 0.0
    error: str = ""
    run_dir: str = ""
    attempts: list[SpinAttempt] = field(default_factory=list)
    samples_per_path: int = 1
    structural_map: dict[str, Any] = field(default_factory=dict)

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
            "discovered_modes": self.discovered_modes,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "run_dir": self.run_dir,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "samples_per_path": self.samples_per_path,
            "structural_map": self.structural_map,
        }
