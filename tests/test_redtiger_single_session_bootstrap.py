from __future__ import annotations

import unittest

from tester_spin.providers.redtiger import execution
from tester_spin.providers.redtiger.bootstrap_browser import _safe_trace_url


class RedTigerSingleSessionBootstrapTests(unittest.TestCase):
    def test_execution_uses_single_session_browser_bootstrap(self) -> None:
        self.assertEqual(
            execution.bootstrap_game.__module__,
            "tester_spin.providers.redtiger.bootstrap_browser",
        )

    def test_trace_url_drops_query_and_redacts_jsessionid(self) -> None:
        sanitized = _safe_trace_url(
            "https://fansite.example/entry;JSESSIONID=secret-value/path?token=secret&x=1"
        )
        self.assertEqual(
            sanitized,
            "https://fansite.example/entry;jsessionid=<redacted>/path",
        )
        self.assertNotIn("token=", sanitized)
        self.assertNotIn("secret-value", sanitized)

    def test_trace_url_redacts_long_opaque_path_segments(self) -> None:
        sanitized = _safe_trace_url(
            "https://g.example/a/" + ("x" * 80) + "/platform/game/settings?session=secret"
        )
        self.assertEqual(
            sanitized,
            "https://g.example/a/<opaque>/platform/game/settings",
        )


if __name__ == "__main__":
    unittest.main()
