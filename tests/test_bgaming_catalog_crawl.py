from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from tester_spin.providers.bgaming.adapter import BGamingProvider


def card(slug: str, name: str, identifier: str) -> str:
    return f"""
    <div data-catalog-card data-image="">
      <a href="https://bgaming.com/games/{slug}"><img alt="{name}"></a>
      <div class="game-type-text">Slots</div>
      <a href="https://demo.bgaming-network.com/play/{identifier}/FUN?server=demo">Play Demo</a>
    </div>
    """


class Response:
    def __init__(self, *, text: str = "", payload=None, status: int = 200):
        self.text = text
        self._payload = payload
        self.status_code = status
        self.url = "https://bgaming.com/test"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code}",
                response=self,
            )

    def json(self):
        return self._payload


class BGamingCatalogCrawlTests(unittest.TestCase):
    def provider(self, temp: str) -> BGamingProvider:
        return BGamingProvider(Path(temp))

    def test_complete_rest_pagination_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = self.provider(temp)
            provider.http.get = unittest.mock.Mock(
                side_effect=[
                    Response(text=card("one", "One", "One")),
                    Response(
                        payload={
                            "page": 2,
                            "total": 2,
                            "hasMore": False,
                            "html": card("two", "Two", "Two"),
                        }
                    ),
                ]
            )
            with patch.object(provider, "_download_thumbnail"):
                games = provider.crawl_catalog(
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                    max_pages=10,
                )
            self.assertEqual([game.slug for game in games], ["one", "two"])
            self.assertTrue(provider.catalog_crawl_authoritative)

    def test_http_failure_keeps_partial_catalog_non_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = self.provider(temp)
            provider.http.get = unittest.mock.Mock(
                side_effect=[
                    Response(text=card("one", "One", "One")),
                    Response(status=500),
                ]
            )
            with patch.object(provider, "_download_thumbnail"):
                games = provider.crawl_catalog(
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                    max_pages=10,
                )
            self.assertEqual([game.slug for game in games], ["one"])
            self.assertFalse(provider.catalog_crawl_authoritative)
            self.assertIn("falló página REST 2", provider.catalog_crawl_reason)

    def test_reported_page_mismatch_is_non_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = self.provider(temp)
            provider.http.get = unittest.mock.Mock(
                side_effect=[
                    Response(text=card("one", "One", "One")),
                    Response(
                        payload={
                            "page": 99,
                            "total": 2,
                            "hasMore": False,
                            "html": card("two", "Two", "Two"),
                        }
                    ),
                ]
            )
            with patch.object(provider, "_download_thumbnail"):
                provider.crawl_catalog(
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                    max_pages=10,
                )
            self.assertFalse(provider.catalog_crawl_authoritative)
            self.assertIn("página REST 2", provider.catalog_crawl_reason)

    def test_total_must_remain_stable_across_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = self.provider(temp)
            provider.http.get = unittest.mock.Mock(
                side_effect=[
                    Response(text=card("one", "One", "One")),
                    Response(
                        payload={
                            "page": 2,
                            "total": 7,
                            "hasMore": True,
                            "html": card("two", "Two", "Two"),
                        }
                    ),
                    Response(
                        payload={
                            "page": 3,
                            "total": 8,
                            "hasMore": False,
                            "html": card("three", "Three", "Three"),
                        }
                    ),
                ]
            )
            with patch.object(provider, "_download_thumbnail"):
                games = provider.crawl_catalog(
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                    max_pages=10,
                )
            self.assertEqual([game.slug for game in games], ["one", "two"])
            self.assertFalse(provider.catalog_crawl_authoritative)
            self.assertIn("total REST cambió", provider.catalog_crawl_reason)

    def test_manual_page_limit_is_never_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = self.provider(temp)
            provider.http.get = unittest.mock.Mock(
                return_value=Response(text=card("one", "One", "One"))
            )
            with patch.object(provider, "_download_thumbnail"):
                provider.crawl_catalog(
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                    max_pages=1,
                )
            self.assertFalse(provider.catalog_crawl_authoritative)


if __name__ == "__main__":
    unittest.main()
