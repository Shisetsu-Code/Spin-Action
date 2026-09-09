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

                CREATE TABLE IF NOT EXISTS catalog_exclusions (
                    provider TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
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

                CREATE INDEX IF NOT EXISTS idx_catalog_exclusions_provider
                ON catalog_exclusions(provider, slug);
                """
            )

    def upsert_games(self, games: list[Game]) -> int:
        if not games:
            return 0
        now = utc_now_iso()
        providers = sorted({game.provider for game in games if game.provider})
        with self._lock, self._connect() as con:
            excluded: set[tuple[str, str]] = set()
            if providers:
                placeholders = ",".join("?" for _ in providers)
                excluded = {
                    (str(row["provider"]), str(row["slug"]))
                    for row in con.execute(
                        f"SELECT provider, slug FROM catalog_exclusions WHERE provider IN ({placeholders})",
                        providers,
                    ).fetchall()
                }

            rows = []
            for game in games:
                if (game.provider, game.slug) in excluded:
                    continue
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

            if rows:
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

    def reconcile_provider_games(self, provider: str, valid_slugs: set[str]) -> int:
        """Remove stale catalogue rows while preserving historical test results.

        This must only be called after a complete provider crawl.  A temporary
        table avoids SQLite's host-parameter limit if a provider eventually has
        more than ~999 catalogue entries. Files under ``data/providers`` and rows
        in ``test_results`` are deliberately untouched.
        """
        normalized = sorted({str(slug).strip() for slug in valid_slugs if str(slug).strip()})
        with self._lock, self._connect() as con:
            con.execute("CREATE TEMP TABLE IF NOT EXISTS _current_catalog_slugs (slug TEXT PRIMARY KEY)")
            con.execute("DELETE FROM _current_catalog_slugs")
            if normalized:
                con.executemany(
                    "INSERT OR IGNORE INTO _current_catalog_slugs(slug) VALUES (?)",
                    ((slug,) for slug in normalized),
                )
                cursor = con.execute(
                    """
                    DELETE FROM games
                    WHERE provider=?
                      AND NOT EXISTS (
                          SELECT 1 FROM _current_catalog_slugs current
                          WHERE current.slug=games.slug
                      )
                    """,
                    (provider,),
                )
            else:
                cursor = con.execute("DELETE FROM games WHERE provider=?", (provider,))
            removed = max(0, int(cursor.rowcount or 0))
            con.execute("DROP TABLE _current_catalog_slugs")
        return removed

    def delete_games(self, provider: str, slugs: set[str]) -> int:
        """Delete explicitly identified catalogue rows only.

        Historical test_results and provider artifact folders are untouched.
        Callers must supply slugs already proven invalid by provider-specific
        structural validation; this is not a broad reconciliation API.
        """
        normalized = sorted(
            {str(slug).strip() for slug in slugs if str(slug).strip()}
        )
        if not normalized:
            return 0
        with self._lock, self._connect() as con:
            con.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _delete_catalog_slugs (slug TEXT PRIMARY KEY)"
            )
            con.execute("DELETE FROM _delete_catalog_slugs")
            con.executemany(
                "INSERT OR IGNORE INTO _delete_catalog_slugs(slug) VALUES (?)",
                ((slug,) for slug in normalized),
            )
            cursor = con.execute(
                """
                DELETE FROM games
                WHERE provider=?
                  AND EXISTS (
                      SELECT 1 FROM _delete_catalog_slugs doomed
                      WHERE doomed.slug=games.slug
                  )
                """,
                (provider,),
            )
            removed = max(0, int(cursor.rowcount or 0))
            con.execute("DROP TABLE _delete_catalog_slugs")
        return removed

    def list_catalog_exclusions(self, provider: str) -> set[str]:
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT slug FROM catalog_exclusions WHERE provider=? ORDER BY slug",
                (provider,),
            ).fetchall()
        return {str(row["slug"]) for row in rows}

    def exclude_games(
        self,
        provider: str,
        slugs: set[str],
        *,
        reason: str = "manual",
    ) -> int:
        """Persist a manual catalogue exclusion and remove matching live rows.

        Historical test_results and provider artifacts remain untouched. The exclusion
        prevents a later crawl/fallback from re-inserting the same slug.
        """
        normalized = sorted(
            {str(slug).strip() for slug in slugs if str(slug).strip()}
        )
        if not normalized:
            return 0
        now = utc_now_iso()
        with self._lock, self._connect() as con:
            con.executemany(
                """
                INSERT INTO catalog_exclusions(provider, slug, reason, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, slug) DO UPDATE SET
                    reason=excluded.reason,
                    created_at=excluded.created_at
                """,
                ((provider, slug, reason, now) for slug in normalized),
            )
            con.executemany(
                "DELETE FROM games WHERE provider=? AND slug=?",
                ((provider, slug) for slug in normalized),
            )
        return len(normalized)

    def clear_provider_catalog(
        self,
        provider: str,
        *,
        clear_exclusions: bool = False,
    ) -> tuple[int, int]:
        """Clear only the live catalogue for one provider.

        Historical test_results are preserved. Manual exclusions are preserved by
        default so known false positives do not immediately return on the next crawl.
        """
        with self._lock, self._connect() as con:
            cursor = con.execute("DELETE FROM games WHERE provider=?", (provider,))
            removed = max(0, int(cursor.rowcount or 0))
            cleared_exclusions = 0
            if clear_exclusions:
                cursor = con.execute(
                    "DELETE FROM catalog_exclusions WHERE provider=?",
                    (provider,),
                )
                cleared_exclusions = max(0, int(cursor.rowcount or 0))
        return removed, cleared_exclusions

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
