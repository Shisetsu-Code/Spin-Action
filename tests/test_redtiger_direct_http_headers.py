from __future__ import annotations

import unittest
from types import SimpleNamespace

import requests

from tester_spin.providers.redtiger.execution import _sanitize_direct_http_headers


class RedTigerDirectHTTPHeaderTests(unittest.TestCase):
    def test_http2_pseudo_headers_are_removed_before_requests_transport(self) -> None:
        session = requests.Session()
        session.headers[":authority"] = "gserver.example"
        session.headers[":method"] = "POST"
        session.headers["Origin"] = "https://gserver.example"
        session.headers["X-Provider-Header"] = "kept"

        runtime = SimpleNamespace(session=session)
        _sanitize_direct_http_headers(runtime)

        self.assertNotIn(":authority", session.headers)
        self.assertNotIn(":method", session.headers)
        self.assertEqual(session.headers["Origin"], "https://gserver.example")
        self.assertEqual(session.headers["X-Provider-Header"], "kept")

    def test_cookie_jar_is_not_modified_by_header_sanitization(self) -> None:
        session = requests.Session()
        session.cookies.set("EVOSESSIONID", "opaque", domain=".example.test", path="/")
        session.headers[":authority"] = "example.test"

        runtime = SimpleNamespace(session=session)
        _sanitize_direct_http_headers(runtime)

        self.assertEqual(session.cookies.get("EVOSESSIONID", domain=".example.test", path="/"), "opaque")
        self.assertNotIn(":authority", session.headers)


if __name__ == "__main__":
    unittest.main()
