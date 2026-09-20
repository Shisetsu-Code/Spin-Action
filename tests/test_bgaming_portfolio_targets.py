from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

import requests

from scripts.bgaming_portfolio_targets import (
    PORTFOLIO_URL,
    enumerate_bgaming_portfolio_targets,
)
from tester_spin.providers.bgaming.adapter import BGamingProvider, CATALOG_SEARCH_URL


def card(slug: str, name: str, identifier: str, game_type: str) -> str:
    return f"""
    <div data-catalog-card data-image="">
      <a href="https://bgaming.com/games/{slug}"><img alt="{name}"></a>
      <div class="game-type-text">{game_type}</div>
      <a href="https://demo.bgaming-network.com/play/{identifier}/FUN?server=demo">Play Demo</a>
    </div>
    """


class Response:
    def __init__(self, *, text: str = "", payload=None, status: int = 200):
        self.text = text
        self._payload = payload
        self.status_code = status
        self.url = PORTFOLIO_URL

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)

    def json(self):
        return self._payload


class BGamingPortfolioTargetTests(unittest.TestCase):
    def test_full_portfolio_keeps_non_slot_types_and_omits_game_type_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = BGamingProvider(Path(temp))
            first = card("slot-a", "Slot A", "SlotA", "Slots")
            second = card("plinko-a", "Plinko A", "PlinkoA", "Casual")
            provider.http.get = unittest.mock.Mock(
                side_effect=[
                    Response(text=first),
                    Response(
                        payload={
                            "page": 2,
                            "total": 2,
                            "hasMore": False,
                            "html": second,
                        }
                    ),
                ]
            )

            games = enumerate_bgaming_portfolio_targets(
                provider,
                requested_pages=0,
                stop_event=threading.Event(),
                progress=lambda _message: None,
            )

            self.assertEqual([game.slug for game in games], ["plinko-a", "slot-a"])
            self.assertTrue(provider.catalog_crawl_authoritative)
            self.assertEqual(provider.http.get.call_args_list[0].args[0], PORTFOLIO_URL)
            self.assertEqual(provider.http.get.call_args_list[1].args[0], CATALOG_SEARCH_URL)
            params = provider.http.get.call_args_list[1].kwargs["params"]
            self.assertNotIn("game_type", params)

            casual_meta = provider.game_dir(games[0]) / "game.json"
            self.assertEqual(
                __import__("json").loads(casual_meta.read_text(encoding="utf-8"))["game_type"],
                "Casual",
            )

    def test_portfolio_pagination_failure_is_non_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = BGamingProvider(Path(temp))
            provider.http.get = unittest.mock.Mock(
                side_effect=[
                    Response(text=card("slot-a", "Slot A", "SlotA", "Slots")),
                    Response(status=500),
                ]
            )

            games = enumerate_bgaming_portfolio_targets(
                provider,
                requested_pages=0,
                stop_event=threading.Event(),
                progress=lambda _message: None,
            )

            self.assertEqual([game.slug for game in games], ["slot-a"])
            self.assertFalse(provider.catalog_crawl_authoritative)
            self.assertIn("falló página portfolio REST 2", provider.catalog_crawl_reason)


if __name__ == "__main__":
    unittest.main()
