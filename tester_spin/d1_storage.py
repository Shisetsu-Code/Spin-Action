from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any

import requests

from tester_spin.models import Game, GameTestResult, utc_now_iso


DEFAULT_API_BASE = "https://api.cloudflare.com/client/v4"


@dataclass(frozen=True, slots=True)
class D1StorageConfig:
    account_id: str
    database_id: str
    api_token: str
    api_base: str = DEFAULT_API_BASE

    @classmethod
    def from_environment(cls) -> "D1StorageConfig":
        account_id = os.environ.get("TESTER_SPIN_D1_ACCOUNT_ID", "").strip()
        database_id = os.environ.get("TESTER_SPIN_D1_DATABASE_ID", "").strip()
        api_token = (
            os.environ.get("TESTER_SPIN_D1_API_TOKEN", "").strip()
            or os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
        )
        api_base = os.environ.get("TESTER_SPIN_D1_API_BASE", DEFAULT_API_BASE).strip().rstrip("/")
        missing = [
            name
            for name, value in (
                ("TESTER_SPIN_D1_ACCOUNT_ID", account_id),
                ("TESTER_SPIN_D1_DATABASE_ID", database_id),
                ("TESTER_SPIN_D1_API_TOKEN/CLOUDFLARE_API_TOKEN", api_token),
            )
            if not value
        ]
        if missing:
            raise RuntimeError("D1 no configurado; faltan: " + ", ".join(missing))
        return cls(
            account_id=account_id,
            database_id=database_id,
            api_token=api_token,
            api_base=api_base,
        )


_D1_LOCAL = threading.local()


class D1Storage:
    """Cloudflare D1 storage backend with the same surface as local Storage.

    The desktop client uses Cloudflare's parameterized D1 REST query API. API
    credentials are read only from environment variables and are never persisted
    by Tester-Spin. For a distributed/remote deployment, the same storage surface
    can later be backed by a Worker binding without changing the GUI/provider code.
    """

    display_name = "Cloudflare D1"

    def __init__(self, config: D1StorageConfig) -> None:
        self.config = config
        self._schema_lock = threading.RLock()
        self._initialize()

    @classmethod
    def from_environment(cls) -> "D1Storage":
        return cls(D1StorageConfig.from_environment())

    @property
    def endpoint(self) -> str:
        return (
            f"{self.config.api_base}/accounts/{self.config.account_id}"
            f"/d1/database/{self.config.database_id}/query"
        )

    def _session(self) -> requests.Session:
        session = getattr(_D1_LOCAL, "session", None)
        signature = getattr(_D1_LOCAL, "signature", None)
        current = (self.config.api_token, self.config.api_base)
        if session is None or signature != current:
            session = requests.Session()
            session.headers.update(
                {
                    "Authorization": f"Bearer {self.config.api_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Tester-Spin-D1/1",
                }
            )
            _D1_LOCAL.session = session
            _D1_LOCAL.signature = current
        return session

    @staticmethod
    def _param(value: Any) -> Any:
        if isinstance(value, bool):
            return 1 if value else 0
        return value

    def _request(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        response = self._session().post(self.endpoint, json=payload, timeout=30.0)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("D1 devolvió una respuesta JSON inválida")
        if data.get("success") is False:
            errors = data.get("errors") or []
            raise RuntimeError(f"D1 API error: {errors}")
        result = data.get("result")
        if not isinstance(result, list):
            raise RuntimeError("D1 API no devolvió result[]")
        for item in result:
            if isinstance(item, dict) and item.get("success") is False:
                raise RuntimeError(f"D1 query falló: {item}")
        return [item for item in result if isinstance(item, dict)]

    def _query(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> dict[str, Any]:
        result = self._request(
            {
                "sql": sql,
                "params": [self._param(value) for value in params],
            }
        )
        return result[0] if result else {"results": [], "meta": {}}

    def _batch(self, statements: list[tuple[str, list[Any] | tuple[Any, ...]]]) -> list[dict[str, Any]]:
        if not statements:
            return []
        return self._request(
            {
                "batch": [
                    {
                        "sql": sql,
                        "params": [self._param(value) for value in params],
                    }
                    for sql, params in statements
                ]
            }
        )

    @staticmethod
    def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
        rows = result.get("results") or []
        return [row for row in rows if isinstance(row, dict)]

    def _initialize(self) -> None:
        schema = """
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
        with self._schema_lock:
            self._query(schema)

    def upsert_games(self, games: list[Game]) -> int:
        if not games:
            return 0
        now = utc_now_iso()
        sql = """
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
        """
        statements: list[tuple[str, tuple[Any, ...]]] = []
        for game in games:
            statements.append(
                (
                    sql,
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
                    ),
                )
            )
        # Keep REST requests bounded even for large catalogues.
        for offset in range(0, len(statements), 100):
            self._batch(statements[offset : offset + 100])
        return len(statements)

    def reconcile_provider_games(self, provider: str, valid_slugs: set[str]) -> int:
        normalized = {str(slug).strip() for slug in valid_slugs if str(slug).strip()}
        current = {
            str(row.get("slug") or "")
            for row in self._rows(
                self._query("SELECT slug FROM games WHERE provider=?", (provider,))
            )
        }
        stale = sorted(current - normalized)
        delete_sql = "DELETE FROM games WHERE provider=? AND slug=?"
        for offset in range(0, len(stale), 100):
            self._batch(
                [(delete_sql, (provider, slug)) for slug in stale[offset : offset + 100]]
            )
        return len(stale)

    def list_games(self, provider: str) -> list[Game]:
        rows = self._rows(
            self._query(
                """
                SELECT * FROM games
                WHERE provider=?
                ORDER BY name COLLATE NOCASE ASC
                """,
                (provider,),
            )
        )
        return [self._row_to_game(row) for row in rows]

    def get_game(self, provider: str, slug: str) -> Game | None:
        rows = self._rows(
            self._query(
                "SELECT * FROM games WHERE provider=? AND slug=? LIMIT 1",
                (provider, slug),
            )
        )
        return self._row_to_game(rows[0]) if rows else None

    def update_thumbnail(self, provider: str, slug: str, thumbnail_path: str) -> None:
        self._query(
            """
            UPDATE games SET thumbnail_path=?, updated_at=?
            WHERE provider=? AND slug=?
            """,
            (thumbnail_path, utc_now_iso(), provider, slug),
        )

    def update_symbol(self, provider: str, slug: str, symbol: str) -> None:
        if not symbol:
            return
        self._query(
            """
            UPDATE games SET symbol=?, updated_at=?
            WHERE provider=? AND slug=?
            """,
            (symbol, utc_now_iso(), provider, slug),
        )

    def record_result(self, result: GameTestResult) -> None:
        payload = json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":"))
        insert_sql = """
        INSERT INTO test_results (
            provider, slug, game_name, game_url, status, symbol,
            requested_spins, successful_spins, failed_spins,
            started_at, finished_at, elapsed_ms, error, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        update_sql = """
        UPDATE games SET
            symbol=CASE WHEN ? <> '' THEN ? ELSE symbol END,
            last_status=?,
            last_error=?,
            last_test_at=?,
            last_latency_ms=?,
            updated_at=?
        WHERE provider=? AND slug=?
        """
        self._batch(
            [
                (
                    insert_sql,
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
                ),
                (
                    update_sql,
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
                ),
            ]
        )

    @staticmethod
    def _row_to_game(row: dict[str, Any]) -> Game:
        latency = row.get("last_latency_ms")
        return Game(
            provider=str(row.get("provider") or ""),
            slug=str(row.get("slug") or ""),
            name=str(row.get("name") or ""),
            url=str(row.get("url") or ""),
            thumbnail_url=str(row.get("thumbnail_url") or ""),
            thumbnail_path=str(row.get("thumbnail_path") or ""),
            symbol=str(row.get("symbol") or ""),
            discovered_at=str(row.get("discovered_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
            last_status=str(row.get("last_status") or "PENDIENTE"),
            last_error=str(row.get("last_error") or ""),
            last_test_at=str(row.get("last_test_at") or ""),
            last_latency_ms=float(latency) if latency is not None else None,
        )
