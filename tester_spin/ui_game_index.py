from __future__ import annotations

from datetime import datetime
from typing import Iterable

from tester_spin.models import Game


STATUS_ORDER = {
    "ERROR": 0,
    "PARCIAL": 1,
    "PENDIENTE": 2,
    "": 2,
    "OK": 3,
}


def retryable_games(games: Iterable[Game]) -> list[Game]:
    """Return games whose latest persisted result is not OK."""
    return [
        game
        for game in games
        if str(game.last_status or "PENDIENTE").strip().upper() != "OK"
    ]


def game_sort_key(game: Game, column: str):
    """Stable, typed sort keys for the GUI game index."""
    column = str(column or "name")
    if column == "name":
        return (str(game.name or "").casefold(), str(game.slug or "").casefold())
    if column == "symbol":
        value = str(game.symbol or "")
        return (not bool(value), value.casefold(), str(game.name or "").casefold())
    if column == "status":
        status = str(game.last_status or "PENDIENTE").strip().upper()
        return (STATUS_ORDER.get(status, 2), status, str(game.name or "").casefold())
    if column == "tested":
        raw = str(game.last_test_at or "").strip()
        if not raw:
            return (1, 0.0, str(game.name or "").casefold())
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except Exception:
            stamp = 0.0
        return (0, stamp, str(game.name or "").casefold())
    if column == "url":
        return (str(game.url or "").casefold(), str(game.name or "").casefold())
    return (str(game.name or "").casefold(), str(game.slug or "").casefold())
