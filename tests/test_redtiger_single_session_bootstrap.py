from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tester_spin.providers.redtiger import evolution_launch
from tester_spin.providers.redtiger.bootstrap_browser import _safe_trace_url


class RedTigerSingleSessionBootstrapTests(unittest.TestCase):
    def test_execution_uses_single_session_browser_bootstrap(self) -> None:
        sentinel = object()
        calls: list[tuple[str, str]] = []

        def fake_browser_bootstrap(public_url, table_id, **_kwargs):
            calls.append((public_url, table_id))
            self.assertIs(
                evolution_launch._browser_bootstrap._wait_for_official_loader,
                evolution_launch._wait_for_live_start_contract,
            )
            self.assertIs(
                evolution_launch._browser_bootstrap._submit_official_start,
                evolution_launch._submit_live_start,
            )
            return sentinel

        old_wait = evolution_launch._browser_bootstrap._wait_for_official_loader
        old_submit = evolution_launch._browser_bootstrap._submit_official_start
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            evolution_launch._browser_bootstrap,
            "bootstrap_game",
            side_effect=fake_browser_bootstrap,
        ) as delegated:
            result = evolution_launch.bootstrap_game(
                "https://games.evolution.com/slots/example/",
                "12345",
                timeout_s=15.0,
                artifact_dir=Path(tmp),
            )

        self.assertIs(result, sentinel)
        self.assertEqual(calls, [("https://games.evolution.com/slots/example/", "12345")])
        self.assertEqual(delegated.call_count, 1)
        self.assertIs(evolution_launch._browser_bootstrap._wait_for_official_loader, old_wait)
        self.assertIs(evolution_launch._browser_bootstrap._submit_official_start, old_submit)

    def test_trace_url_redacts_query_values_and_jsessionid(self) -> None:
        sanitized = _safe_trace_url(
            "https://fansite.example/entry;JSESSIONID=secret-value/path?token=secret&x=1"
        )
        self.assertEqual(
            sanitized,
            "https://fansite.example/entry;jsessionid=<redacted>/path?token=<redacted>&x=<redacted>",
        )
        self.assertNotIn("secret-value", sanitized)
        self.assertNotIn("token=secret", sanitized)

    def test_trace_url_redacts_long_opaque_path_segments_and_query_values(self) -> None:
        sanitized = _safe_trace_url(
            "https://g.example/a/" + ("x" * 80) + "/platform/game/settings?session=secret"
        )
        self.assertEqual(
            sanitized,
            "https://g.example/a/<opaque>/platform/game/settings?session=<redacted>",
        )
        self.assertNotIn("session=secret", sanitized)


if __name__ == "__main__":
    unittest.main()
