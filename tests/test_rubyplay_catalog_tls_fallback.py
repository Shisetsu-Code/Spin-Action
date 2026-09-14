from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from tester_spin.providers.rubyplay.exhaustive import RubyPlayProvider


BRICKS_HTML = r'''
<script>
window.bricksData = {
  restApiUrl: "https://rubyplay.com/wp-json/bricks/v1/",
  nonce: "query-nonce",
  wpRestNonce: "rest-nonce",
  postId: "10149",
  language: "en"
};
</script>
<div data-query-element-id="alpha"
     data-query-vars='{"post_type":["games"],"posts_per_page":1,"paged":1}'
     data-page="1" data-max-pages="1" data-start="1" data-end="1"></div>
<!--brx-loop-start-alpha-->
  <div><a href="/games/alpha-1/">Alpha 1</a></div>
<!--brx-loop-end-alpha-->
'''

DOM_HTML = r'''
<html><body>
  <article><a href="https://rubyplay.com/games/volcano-rising-se/">Volcano Rising SE</a></article>
  <article><a href="/games/go-high-panda/">Go High Panda</a></article>
  <a href="https://rubyplay.com/es/games/">ES</a>
  <a href="https://example.com/games/not-rubyplay/">Other</a>
</body></html>
'''


class _TlsFailSession:
    def get(self, *_args, **_kwargs):
        raise requests.exceptions.SSLError("unable to get local issuer certificate")


class _HttpDomSession:
    def __init__(self) -> None:
        self.get_calls = 0

    def get(self, url: str, *_args, **_kwargs):
        self.get_calls += 1
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response._content = DOM_HTML.encode("utf-8")
        response.encoding = "utf-8"
        return response


class _BrowserCatalog:
    instances = []
    html = BRICKS_HTML

    def __init__(self, catalog_url: str, *, timeout_s: float = 30.0):
        self.catalog_url = catalog_url
        self.timeout_s = timeout_s
        self.started = False
        self.closed = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def fetch_catalog_html(self) -> tuple[str, str]:
        if not self.started:
            raise AssertionError("browser must be started before reading catalog HTML")
        return self.__class__.html, self.catalog_url

    def request_json(self, *_args, **_kwargs):
        raise AssertionError("one-page catalog must not paginate")

    def fetch_page(self, *_args, **_kwargs):
        raise AssertionError("one-page catalog must not paginate")


class RubyPlayCatalogTlsFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        _BrowserCatalog.instances.clear()
        _BrowserCatalog.html = BRICKS_HTML

    def _crawl(self, provider: RubyPlayProvider, messages: list[str]):
        with patch(
            "tester_spin.providers.rubyplay.exhaustive.RubyPlayBrowserCatalogClient",
            _BrowserCatalog,
        ):
            return provider.crawl_catalog(
                stop_event=threading.Event(),
                progress=messages.append,
                max_pages=0,
            )

    def test_initial_requests_tls_failure_uses_browser_without_disabling_tls(self) -> None:
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as temp:
            provider = RubyPlayProvider(Path(temp))
            provider.http = _TlsFailSession()  # type: ignore[assignment]
            games = self._crawl(provider, messages)

        self.assertEqual([game.slug for game in games], ["alpha-1"])
        self.assertEqual(len(_BrowserCatalog.instances), 1)
        browser = _BrowserCatalog.instances[0]
        self.assertTrue(browser.started)
        self.assertTrue(browser.closed)
        self.assertTrue(any("TLS" in message for message in messages))
        self.assertTrue(any("Chromium" in message for message in messages))
        self.assertTrue(provider.catalog_crawl_authoritative)

    def test_current_dom_without_bricks_query_yields_strict_game_targets_fail_closed(self) -> None:
        _BrowserCatalog.html = DOM_HTML
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as temp:
            provider = RubyPlayProvider(Path(temp))
            provider.http = _TlsFailSession()  # type: ignore[assignment]
            games = self._crawl(provider, messages)

        self.assertEqual(
            sorted(game.slug for game in games),
            ["go-high-panda", "volcano-rising-se"],
        )
        self.assertFalse(provider.catalog_crawl_authoritative)
        self.assertIn("DOM", provider.catalog_crawl_reason)
        self.assertTrue(any("DOM" in message for message in messages))

    def test_http_200_current_dom_reuses_saved_html_without_second_request(self) -> None:
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as temp:
            provider = RubyPlayProvider(Path(temp))
            session = _HttpDomSession()
            provider.http = session  # type: ignore[assignment]
            games = self._crawl(provider, messages)

        self.assertEqual(
            sorted(game.slug for game in games),
            ["go-high-panda", "volcano-rising-se"],
        )
        self.assertEqual(session.get_calls, 1)
        self.assertEqual(_BrowserCatalog.instances, [])
        self.assertFalse(provider.catalog_crawl_authoritative)
        self.assertIn("DOM", provider.catalog_crawl_reason)
        self.assertTrue(any("DOM" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
