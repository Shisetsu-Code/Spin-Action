from __future__ import annotations

import unittest

from tester_spin.models import Game
from tester_spin.ui_game_index import game_sort_key, retryable_games


class GameIndexTests(unittest.TestCase):
    @staticmethod
    def game(name: str, status: str, tested: str = "", symbol: str = "") -> Game:
        return Game(
            provider="pragmatic",
            slug=name.lower().replace(" ", "-"),
            name=name,
            url=f"https://example.test/{name}",
            symbol=symbol,
            last_status=status,
            last_test_at=tested,
        )

    def test_retryable_excludes_only_ok(self) -> None:
        games = [
            self.game("Good", "OK"),
            self.game("Partial", "PARCIAL"),
            self.game("Broken", "ERROR"),
            self.game("Never", "PENDIENTE"),
            self.game("Blank", ""),
        ]
        retry = retryable_games(games)
        self.assertEqual(
            [game.name for game in retry],
            ["Partial", "Broken", "Never", "Blank"],
        )

    def test_status_sort_puts_work_before_ok(self) -> None:
        games = [
            self.game("Good", "OK"),
            self.game("Never", "PENDIENTE"),
            self.game("Partial", "PARCIAL"),
            self.game("Broken", "ERROR"),
        ]
        ordered = sorted(games, key=lambda game: game_sort_key(game, "status"))
        self.assertEqual(
            [game.last_status for game in ordered],
            ["ERROR", "PARCIAL", "PENDIENTE", "OK"],
        )

    def test_timestamp_sort_is_chronological(self) -> None:
        older = self.game("Older", "OK", "2026-08-27T10:00:00+00:00")
        newer = self.game("Newer", "OK", "2026-08-28T10:00:00+00:00")
        ordered = sorted([newer, older], key=lambda game: game_sort_key(game, "tested"))
        self.assertEqual([game.name for game in ordered], ["Older", "Newer"])

    def test_name_sort_is_case_insensitive(self) -> None:
        games = [self.game("zeta", "OK"), self.game("Alpha", "OK")]
        ordered = sorted(games, key=lambda game: game_sort_key(game, "name"))
        self.assertEqual([game.name for game in ordered], ["Alpha", "zeta"])


if __name__ == "__main__":
    unittest.main()
