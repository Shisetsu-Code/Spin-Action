from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from tester_spin.providers.rubyplay.exhaustive import RubyPlayProvider


HTML = r'''
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


class _TlsFailSession:
    def get(self, *_args, **_kwargs):
        raise requests.exceptions.SSLError("unable to get local issuer certificate")


class _BrowserCatalog:
    instances = []

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
        return HTML, self.catalog_url

    def request_json(self, *_args, **_kwargs):
        raise AssertionError("one-page catalog must not paginate")

    def fetch_page(self, *_args, **_kwargs):
        raise AssertionError("one-page catalog must not paginate")


class RubyPlayCatalogTlsFallbackTests(unittest.TestCase):
    def test_initial_requests_tls_failure_uses_browser_without_disabling_tls(self) -> None:
        _BrowserCatalog.instances.clear()
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as temp:
            provider = RubyPlayProvider(Path(temp))
            provider.http = _TlsFailSession()  # type: ignore[assignment]
            with patch(
                "tester_spin.providers.rubyplay.exhaustive.RubyPlayBrowserCatalogClient",
                _BrowserCatalog,
            ):
                games = provider.crawl_catalog(
                    stop_event=threading.Event(),
                    progress=messages.append,
                    max_pages=0,
                )

        self.assertEqual([game.slug for game in games], ["alpha-1"])
        self.assertEqual(len(_BrowserCatalog.instances), 1)
        browser = _BrowserCatalog.instances[0]
        self.assertTrue(browser.started)
        self.assertTrue(browser.closed)
        self.assertTrue(any("TLS" in message for message in messages))
        self.assertTrue(any("Chromium" in message for message in messages))
        self.assertTrue(provider.catalog_crawl_authoritative)


if __name__ == "__main__":
    unittest.main()
