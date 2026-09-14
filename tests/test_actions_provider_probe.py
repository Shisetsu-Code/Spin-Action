from __future__ import annotations

import threading
import unittest

from scripts.actions_provider_probe import (
    build_direct_game,
    build_parser,
    enumerate_catalog_for_probe,
    provider_class_for,
    select_games,
    summarize_audits,
)
from tester_spin.models import Game


class _CatalogProvider:
    key = "test-provider"

    def __init__(self) -> None:
        self.downloads = 0

    def _download_thumbnail(self, game, progress) -> None:
        self.downloads += 1

    def catalog_record_invalid_reason(self, game) -> str:
        return ""

    def crawl_catalog(self, *, stop_event, progress, max_pages, on_game=None):
        game = Game(self.key, "alpha", "Alpha", "https://example.invalid/alpha")
        self._download_thumbnail(game, progress)
        return [game]


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
            "1spin4win",
        }
        resolved = {key: provider_class_for(key).key for key in expected}
        self.assertEqual(resolved, {key: key for key in expected})
        self.assertEqual(provider_class_for("one_spin4win").key, "1spin4win")

    def test_probe_catalog_enumeration_uses_low_traffic_manifest_path(self) -> None:
        provider = _CatalogProvider()
        games = enumerate_catalog_for_probe(
            provider,
            requested_pages=1,
            stop_event=threading.Event(),
            progress=lambda _message: None,
        )
        self.assertEqual([game.slug for game in games], ["alpha"])
        self.assertEqual(provider.downloads, 0)

    def test_direct_target_builds_game_without_catalog(self) -> None:
        game = build_direct_game(
            provider_key="pragmatic",
            slug="777-wheel-blitz",
            name="777 Wheel Blitz",
            url="https://www.pragmaticplay.com/en/games/777-wheel-blitz/",
            symbol="vs5wheel7s",
        )
        self.assertEqual(game.provider, "pragmatic")
        self.assertEqual(game.slug, "777-wheel-blitz")
        self.assertEqual(game.symbol, "vs5wheel7s")

    def test_direct_target_requires_slug_and_url(self) -> None:
        with self.assertRaises(ValueError):
            build_direct_game(
                provider_key="pragmatic",
                slug="",
                name="Demo",
                url="https://example.invalid/demo",
                symbol="",
            )
        with self.assertRaises(ValueError):
            build_direct_game(
                provider_key="pragmatic",
                slug="demo",
                name="Demo",
                url="",
                symbol="",
            )

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
