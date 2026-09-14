from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tester_spin.providers.redtiger import bootstrap_browser, evolution_launch
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

    def test_trace_response_headers_allowlists_only_safe_edge_metadata(self) -> None:
        sanitized = bootstrap_browser._safe_response_headers(
            {
                "Server": "cloudflare",
                "Content-Type": "text/html; charset=UTF-8",
                "X-Frame-Options": "SAMEORIGIN",
                "Content-Security-Policy": "frame-ancestors 'self' https://showcase.evo-games.com",
                "Cross-Origin-Resource-Policy": "same-site",
                "CF-Mitigated": "challenge",
                "Set-Cookie": "session=do-not-record",
                "Authorization": "Bearer do-not-record",
            }
        )
        self.assertEqual(
            sanitized,
            {
                "cf-mitigated": "challenge",
                "content-security-policy": "frame-ancestors 'self' https://showcase.evo-games.com",
                "content-type": "text/html; charset=UTF-8",
                "cross-origin-resource-policy": "same-site",
                "server": "cloudflare",
                "x-frame-options": "SAMEORIGIN",
            },
        )
        self.assertNotIn("set-cookie", sanitized)
        self.assertNotIn("authorization", sanitized)
        self.assertNotIn("do-not-record", repr(sanitized))

    def test_error_page_fingerprint_classifies_without_persisting_body(self) -> None:
        body = "<html><head><title>Just a moment...</title></head><body>cf-chl challenge secret-token</body></html>"
        fingerprint = bootstrap_browser._safe_error_page_fingerprint(body)
        self.assertEqual(fingerprint["kind"], "cloudflare_challenge")
        self.assertEqual(fingerprint["body_bytes"], len(body.encode("utf-8")))
        self.assertRegex(fingerprint["sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("body", fingerprint)
        self.assertNotIn("secret-token", repr(fingerprint))


if __name__ == "__main__":
    unittest.main()
