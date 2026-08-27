from __future__ import annotations

import unittest

from tester_spin.providers.pragmatic_safe import _safe_request_wire_text


class _BinaryRequest:
    url = "https://example.test/gameService?fallback=1"

    @property
    def post_data_buffer(self):
        return b"action=doInit&symbol=vs-test&opaque=\xff\xfe"

    @property
    def post_data(self):
        raise AssertionError("post_data must not be used when a binary buffer exists")


class _BrokenTextRequest:
    url = "https://example.test/gameService?action=doInit&symbol=from-query"

    @property
    def post_data_buffer(self):
        raise TypeError("simulated Playwright decode wrapper failure")

    @property
    def post_data(self):
        raise TypeError("simulated Playwright decode wrapper failure")


class PragmaticRequestWireTests(unittest.TestCase):
    def test_invalid_utf8_body_is_decoded_lossily_without_raising(self) -> None:
        text = _safe_request_wire_text(_BinaryRequest())
        self.assertIn("action=doInit", text)
        self.assertIn("symbol=vs-test", text)
        self.assertIn("\ufffd", text)

    def test_query_is_used_when_playwright_body_access_fails(self) -> None:
        text = _safe_request_wire_text(_BrokenTextRequest())
        self.assertEqual(text, "action=doInit&symbol=from-query")


if __name__ == "__main__":
    unittest.main()
