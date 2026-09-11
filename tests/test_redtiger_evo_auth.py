from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlparse

from tester_spin.providers.redtiger.evo_auth import (
    client_version_from_html,
    entry_json_url,
    loader_target_url,
)


class RedTigerEvolutionAuthTests(unittest.TestCase):
    def test_client_version_is_read_from_live_build_meta(self) -> None:
        html = (
            '<html><head><meta content="Build Version: '
            '9.20421130.12345.67890-deadbeef00-r7 at 2042-11-30 12:34:56 UTC" '
            'id="build" name="build"></head></html>'
        )
        self.assertEqual(
            client_version_from_html(html),
            "9.20421130.12345.67890-deadbeef00-r7",
        )

    def test_entry_json_auth_preserves_opaque_session_fields(self) -> None:
        url = entry_json_url(
            "/entry?params=opaque-payload&JSESSIONID=opaque-session&embedded=true",
            "https://fansite.example",
            "8.30000101.11111.22222-abcdef1234-r3",
        )
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "fansite.example")
        self.assertEqual(query["params"], ["opaque-payload"])
        self.assertEqual(query["JSESSIONID"], ["opaque-session"])
        self.assertEqual(query["embedded"], ["true"])
        self.assertEqual(query["json"], ["true"])
        self.assertEqual(query["cc"], ["1"])
        self.assertEqual(
            query["client_version"],
            ["8.30000101.11111.22222-abcdef1234-r3"],
        )

    def test_loader_target_keeps_frontend_path_and_auth_location_shape(self) -> None:
        target = loader_target_url(
            "https://fansite.example/frontend/evo/r3/",
            "https://fansite.example/ignored/path?session_hint=x#demo=direct&provider=redtiger&table_id=opaque-table",
            "https://fansite.example",
        )
        parsed = urlparse(target)
        query = parse_qs(parsed.query)
        fragment = parse_qs(parsed.fragment)
        self.assertEqual(parsed.path, "/frontend/evo/r3/")
        self.assertEqual(query["session_hint"], ["x"])
        self.assertEqual(fragment["demo"], ["direct"])
        self.assertEqual(fragment["provider"], ["redtiger"])
        self.assertEqual(fragment["table_id"], ["opaque-table"])
        self.assertEqual(fragment["origin"], ["https://fansite.example"])

    def test_entry_outside_live_origin_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            entry_json_url(
                "https://unrelated.example/entry?params=x",
                "https://fansite.example",
                "8.30000101.11111.22222-abcdef1234-r3",
            )


if __name__ == "__main__":
    unittest.main()
