from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from tester_spin.providers.rubyplay.browser_http import RubyPlayTlsFallbackSession
from tester_spin.providers.rubyplay.exhaustive import RubyPlayProvider


class _BaseSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.closed = False

    def get(self, url: str, **_kwargs):
        if url.startswith("https://rubyplay.com/"):
            raise requests.exceptions.SSLError("unable to get local issuer certificate")
        raise AssertionError(f"unexpected URL: {url}")

    def post(self, *_args, **_kwargs):
        raise AssertionError("post not expected")

    def close(self) -> None:
        self.closed = True


class _Response:
    status_code = 200
    url = "https://rubyplay.com/games/example/"
    text = '<iframe src="https://launcher.example/launcher?gamename=x&server_url=https%3A%2F%2Fserver.example&currency=FUN&mode=demo&operator=demo&lang=en"></iframe>'
    content = text.encode("utf-8")

    def raise_for_status(self) -> None:
        return None


class _BrowserTransport:
    instances = []

    def __init__(self) -> None:
        self.closed = False
        self.calls: list[str] = []
        self.__class__.instances.append(self)

    def get(self, url: str, *, timeout_s: float):
        self.calls.append(url)
        self.timeout_s = timeout_s
        return _Response()

    def close(self) -> None:
        self.closed = True


class _ExclusiveBrowserTransport:
    active = 0
    instances = []

    def __init__(self) -> None:
        if self.__class__.active:
            raise RuntimeError("nested sync browser transport")
        self.__class__.active += 1
        self.closed = False
        self.__class__.instances.append(self)

    def get(self, _url: str, *, timeout_s: float):
        self.timeout_s = timeout_s
        return _Response()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.__class__.active -= 1


class RubyPlayGameTlsFallbackTests(unittest.TestCase):
    def test_provider_session_falls_back_lazily_only_after_requests_ssl_error(self) -> None:
        _BrowserTransport.instances.clear()
        base = _BaseSession()
        with patch(
            "tester_spin.providers.rubyplay.exhaustive._RubyPlayProvider._new_session",
            return_value=base,
        ), patch(
            "tester_spin.providers.rubyplay.exhaustive.RubyPlayVerifiedBrowserTransport",
            _BrowserTransport,
        ):
            provider = RubyPlayProvider.__new__(RubyPlayProvider)
            session = provider._new_session()
            self.assertEqual(_BrowserTransport.instances, [])
            response = session.get(
                "https://rubyplay.com/games/example/",
                timeout=12.5,
                allow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(_BrowserTransport.instances), 1)
            self.assertEqual(
                _BrowserTransport.instances[0].calls,
                ["https://rubyplay.com/games/example/"],
            )
            self.assertTrue(
                _BrowserTransport.instances[0].closed,
                "verified-browser TLS fallback must not keep a Sync Playwright transport alive",
            )
            session.close()

        self.assertTrue(base.closed)
        self.assertTrue(_BrowserTransport.instances[0].closed)

    def test_sequential_tls_fallback_sessions_do_not_overlap_sync_browser_transports(self) -> None:
        _ExclusiveBrowserTransport.active = 0
        _ExclusiveBrowserTransport.instances.clear()
        first = RubyPlayTlsFallbackSession(
            _BaseSession(),
            transport_factory=_ExclusiveBrowserTransport,
        )
        second = RubyPlayTlsFallbackSession(
            _BaseSession(),
            transport_factory=_ExclusiveBrowserTransport,
        )
        try:
            self.assertEqual(
                first.get("https://rubyplay.com/games/one/", timeout=10).status_code,
                200,
            )
            self.assertEqual(_ExclusiveBrowserTransport.active, 0)
            self.assertEqual(
                second.get("https://rubyplay.com/games/two/", timeout=10).status_code,
                200,
            )
            self.assertEqual(_ExclusiveBrowserTransport.active, 0)
        finally:
            first.close()
            second.close()


if __name__ == "__main__":
    unittest.main()
