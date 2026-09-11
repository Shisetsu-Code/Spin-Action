from __future__ import annotations

import unittest

from tester_spin.providers.redtiger.bootstrap_browser import (
    _embedded_entry_url,
    _header_shape,
    _safe_trace_url,
)


class RedTigerEntryFallbackTests(unittest.TestCase):
    def test_safe_trace_url_keeps_query_names_but_not_values(self) -> None:
        value = _safe_trace_url(
            "https://fansite.example/entry;jsessionid=secret/path?token=abc123&lang=en&foo=bar"
        )
        self.assertIn(";jsessionid=<redacted>", value)
        self.assertIn("foo=<redacted>", value)
        self.assertIn("lang=<redacted>", value)
        self.assertIn("token=<redacted>", value)
        self.assertNotIn("abc123", value)
        self.assertNotIn("secret/path", value)

    def test_header_shape_exposes_only_names_and_cookie_names(self) -> None:
        shape = _header_shape(
            {
                "User-Agent": "browser",
                "Cookie": "JSESSIONID=sensitive; another_cookie=also-sensitive",
                "Referer": "https://example.test/",
            }
        )
        self.assertEqual(shape["cookie_names"], ["JSESSIONID", "another_cookie"])
        self.assertIn("cookie", shape["header_names"])
        self.assertNotIn("sensitive", str(shape))

    def test_embedded_entry_comes_only_from_live_token_response(self) -> None:
        entries = {"entryEmbedded": "/embedded/session-value"}
        url = _embedded_entry_url(entries, "https://fansite.example")
        self.assertEqual(url, "https://fansite.example/embedded/session-value")
        self.assertEqual(_embedded_entry_url({}, "https://fansite.example"), "")

    def test_same_denied_url_is_not_retried_as_embedded_fallback(self) -> None:
        entries = {"entryEmbedded": "https://fansite.example/entry"}
        self.assertEqual(
            _embedded_entry_url(
                entries,
                "https://fansite.example",
                "https://fansite.example/entry",
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()
