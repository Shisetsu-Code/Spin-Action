from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from tester_spin.models import Game, GameTestResult, utc_now_iso


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    def _initialize(self) -> None:
        with self._lock, self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS games (
                    provider TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    thumbnail_path TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_status TEXT NOT NULL DEFAULT 'PENDIENTE',
                    last_error TEXT NOT NULL DEFAULT '',
                    last_test_at TEXT NOT NULL DEFAULT '',
                    last_latency_ms REAL,
                    PRIMARY KEY(provider, slug)
                );

                CREATE TABLE IF NOT EXISTS test_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    game_name TEXT NOT NULL,
                    game_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    requested_spins INTEGER NOT NULL,
                    successful_spins INTEGER NOT NULL,
                    failed_spins INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    elapsed_ms REAL NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_games_provider_name
                ON games(provider, name COLLATE NOCASE);

                CREATE INDEX IF NOT EXISTS idx_results_provider_slug
                ON test_results(provider, slug, id DESC);
                """
            )

    def upsert_games(self, games: list[Game]) -> int:
        if not games:
            return 0
        now = utc_now_iso()
        rows = []
        for game in games:
            rows.append(
                (
                    game.provider,
                    game.slug,
                    game.name,
                    game.url,
                    game.thumbnail_url,
                    game.thumbnail_path,
                    game.symbol,
                    game.discovered_at or now,
                    now,
                )
            )
        with self._lock, self._connect() as con:
            con.executemany(
                """
                INSERT INTO games (
                    provider, slug, name, url, thumbnail_url, thumbnail_path,
                    symbol, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, slug) DO UPDATE SET
                    name=excluded.name,
                    url=excluded.url,
                    thumbnail_url=CASE
                        WHEN excluded.thumbnail_url <> '' THEN excluded.thumbnail_url
                        ELSE games.thumbnail_url
                    END,
                    thumbnail_path=CASE
                        WHEN excluded.thumbnail_path <> '' THEN excluded.thumbnail_path
                        ELSE games.thumbnail_path
                    END,
                    symbol=CASE
                        WHEN excluded.symbol <> '' THEN excluded.symbol
                        ELSE games.symbol
                    END,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def list_games(self, provider: str) -> list[Game]:
        with self._lock, self._connect() as con:
            rows = con.execute(
                """
                SELECT * FROM games
                WHERE provider=?
                ORDER BY name COLLATE NOCASE ASC
                """,
                (provider,),
            ).fetchall()
        return [self._row_to_game(row) for row in rows]

    def get_game(self, provider: str, slug: str) -> Game | None:
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT * FROM games WHERE provider=? AND slug=?",
                (provider, slug),
            ).fetchone()
        return None if row is None else self._row_to_game(row)

    def update_thumbnail(self, provider: str, slug: str, thumbnail_path: str) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                """
                UPDATE games SET thumbnail_path=?, updated_at=?
                WHERE provider=? AND slug=?
                """,
                (thumbnail_path, utc_now_iso(), provider, slug),
            )

    def update_symbol(self, provider: str, slug: str, symbol: str) -> None:
        if not symbol:
            return
        with self._lock, self._connect() as con:
            con.execute(
                """
                UPDATE games SET symbol=?, updated_at=?
                WHERE provider=? AND slug=?
                """,
                (symbol, utc_now_iso(), provider, slug),
            )

    def record_result(self, result: GameTestResult) -> None:
        payload = json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as con:
            con.execute(
                """
                INSERT INTO test_results (
                    provider, slug, game_name, game_url, status, symbol,
                    requested_spins, successful_spins, failed_spins,
                    started_at, finished_at, elapsed_ms, error, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.provider,
                    result.slug,
                    result.game_name,
                    result.game_url,
                    result.status,
                    result.symbol,
                    result.requested_spins,
                    result.successful_spins,
                    result.failed_spins,
                    result.started_at,
                    result.finished_at,
                    result.elapsed_ms,
                    result.error,
                    payload,
                ),
            )
            con.execute(
                """
                UPDATE games SET
                    symbol=CASE WHEN ? <> '' THEN ? ELSE symbol END,
                    last_status=?,
                    last_error=?,
                    last_test_at=?,
                    last_latency_ms=?,
                    updated_at=?
                WHERE provider=? AND slug=?
                """,
                (
                    result.symbol,
                    result.symbol,
                    result.status,
                    result.error,
                    result.finished_at,
                    result.elapsed_ms,
                    utc_now_iso(),
                    result.provider,
                    result.slug,
                ),
            )

    @staticmethod
    def _row_to_game(row: sqlite3.Row) -> Game:
        return Game(
            provider=row["provider"],
            slug=row["slug"],
            name=row["name"],
            url=row["url"],
            thumbnail_url=row["thumbnail_url"],
            thumbnail_path=row["thumbnail_path"],
            symbol=row["symbol"],
            discovered_at=row["discovered_at"],
            updated_at=row["updated_at"],
            last_status=row["last_status"],
            last_error=row["last_error"],
            last_test_at=row["last_test_at"],
            last_latency_ms=row["last_latency_ms"],
        )
