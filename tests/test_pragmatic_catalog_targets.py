from __future__ import annotations

import threading
import unittest

from scripts.pragmatic_catalog_targets import enumerate_pragmatic_targets
from tester_spin.models import Game
from tester_spin.providers.pragmatic_catalog_ajax import AjaxPage


class _Response:
    def __init__(self, text: str = "initial", url: str = "https://example.invalid/games/") -> None:
        self.text = text
        self.url = url

    def raise_for_status(self) -> None:
        return None


class _Http:
    def get(self, url: str, timeout: float):
        return _Response(url=url)


class _Provider:
    catalog_url = "https://example.invalid/games/"

    def __init__(self, initial_games: list[Game], per_page: int = 2) -> None:
        self.http = _Http()
        self.initial_games = initial_games
        self.per_page = per_page

    def _extract_catalog_page(self, html: str, url: str) -> list[Game]:
        return list(self.initial_games)


class PragmaticCatalogTargetsTests(unittest.TestCase):
    def test_short_initial_page_still_validates_ajax_page_two_before_declaring_end(self) -> None:
        provider = _Provider(
            [Game("pragmatic", "one", "One", "https://example.invalid/one")],
            per_page=2,
        )
        calls: list[int] = []

        def fetch(_provider, page: int) -> AjaxPage:
            calls.append(page)
            return AjaxPage(
                page=page,
                url=f"https://example.invalid/games/?page={page}",
                status=200,
                elapsed_ms=1.0,
                games=[Game("pragmatic", "two", "Two", "https://example.invalid/two")],
            )

        games = enumerate_pragmatic_targets(
            provider,
            stop_event=threading.Event(),
            progress=lambda _message: None,
            items_per_page_override=2,
            fetch_ajax=fetch,
        )

        self.assertEqual(calls, [2])
        self.assertEqual([game.slug for game in games], ["one", "two"])

    def test_enumerates_sequentially_until_first_short_ajax_page_without_persisting(self) -> None:
        provider = _Provider(
            [
                Game("pragmatic", "b", "B", "https://example.invalid/b"),
                Game("pragmatic", "a", "A", "https://example.invalid/a"),
            ],
            per_page=2,
        )
        calls: list[int] = []

        def fetch(_provider, page: int) -> AjaxPage:
            calls.append(page)
            if page == 2:
                return AjaxPage(
                    page=2,
                    url="https://example.invalid/games/?page=2",
                    status=200,
                    elapsed_ms=1.0,
                    games=[
                        Game("pragmatic", "c", "C", "https://example.invalid/c"),
                        Game("pragmatic", "d", "D", "https://example.invalid/d"),
                    ],
                )
            return AjaxPage(
                page=3,
                url="https://example.invalid/games/?page=3",
                status=200,
                elapsed_ms=1.0,
                games=[Game("pragmatic", "e", "E", "https://example.invalid/e")],
            )

        games = enumerate_pragmatic_targets(
            provider,
            stop_event=threading.Event(),
            progress=lambda _message: None,
            items_per_page_override=2,
            fetch_ajax=fetch,
        )

        self.assertEqual(calls, [2, 3])
        self.assertEqual([game.slug for game in games], ["a", "b", "c", "d", "e"])

    def test_ajax_failure_fails_closed_instead_of_guessing_catalog_end(self) -> None:
        provider = _Provider(
            [
                Game("pragmatic", "a", "A", "https://example.invalid/a"),
                Game("pragmatic", "b", "B", "https://example.invalid/b"),
            ],
            per_page=2,
        )

        def fetch(_provider, page: int) -> AjaxPage:
            return AjaxPage(
                page=page,
                url="https://example.invalid/games/?page=2",
                status=503,
                elapsed_ms=1.0,
                games=[],
                error="HTTP 503",
            )

        with self.assertRaises(RuntimeError):
            enumerate_pragmatic_targets(
                provider,
                stop_event=threading.Event(),
                progress=lambda _message: None,
                items_per_page_override=2,
                fetch_ajax=fetch,
            )


if __name__ == "__main__":
    unittest.main()
