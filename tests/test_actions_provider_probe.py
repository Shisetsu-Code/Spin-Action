from __future__ import annotations

import unittest

from scripts.actions_provider_probe import (
    build_parser,
    provider_class_for,
    select_games,
    summarize_audits,
)
from tester_spin.models import Game


class ActionsProviderProbeTests(unittest.TestCase):
    def test_har_fallback_is_disabled_by_default(self) -> None:
        args = build_parser().parse_args(["--provider", "pragmatic"])
        self.assertFalse(args.allow_har_fallback)

    def test_every_active_provider_is_addressable(self) -> None:
        expected = {
            "pragmatic",
            "bgaming",
            "rubyplay",
            "redtiger",
            "belatra",
            "one_spin4win",
        }
        resolved = {key: provider_class_for(key).key for key in expected}
        self.assertEqual(resolved, {key: key for key in expected})

    def test_game_selection_is_stable_and_supports_one_by_one_offsets(self) -> None:
        games = [
            Game("p", "z-last", "Z", "https://example.invalid/z"),
            Game("p", "a-first", "A", "https://example.invalid/a"),
            Game("p", "m-middle", "M", "https://example.invalid/m"),
        ]

        first = select_games(games, slug="", offset=0, limit=1)
        second = select_games(games, slug="", offset=1, limit=1)
        all_games = select_games(games, slug="", offset=0, limit=0)

        self.assertEqual([game.slug for game in first], ["a-first"])
        self.assertEqual([game.slug for game in second], ["m-middle"])
        self.assertEqual(
            [game.slug for game in all_games],
            ["a-first", "m-middle", "z-last"],
        )

    def test_slug_selection_is_exact(self) -> None:
        games = [
            Game("p", "alpha", "A", "https://example.invalid/a"),
            Game("p", "beta", "B", "https://example.invalid/b"),
        ]
        selected = select_games(games, slug="beta", offset=0, limit=1)
        self.assertEqual([game.slug for game in selected], ["beta"])

    def test_summary_is_fail_closed(self) -> None:
        self.assertEqual(summarize_audits([]), "UNKNOWN")
        self.assertEqual(
            summarize_audits([{"verdict": "COMPLETE"}, {"verdict": "UNKNOWN"}]),
            "UNKNOWN",
        )
        self.assertEqual(
            summarize_audits([{"verdict": "UNKNOWN"}, {"verdict": "INCOMPLETE"}]),
            "INCOMPLETE",
        )
        self.assertEqual(
            summarize_audits([{"verdict": "COMPLETE"}, {"verdict": "ERROR"}]),
            "ERROR",
        )
        self.assertEqual(
            summarize_audits([{"verdict": "COMPLETE"}, {"verdict": "COMPLETE"}]),
            "COMPLETE",
        )


if __name__ == "__main__":
    unittest.main()
