from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

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
            session.close()

        self.assertTrue(base.closed)
        self.assertTrue(_BrowserTransport.instances[0].closed)


if __name__ == "__main__":
    unittest.main()
