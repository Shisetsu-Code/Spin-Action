from __future__ import annotations

import inspect
import threading
import unittest
from urllib.parse import parse_qs, urlparse

from tester_spin.providers.redtiger import evo_auth
from tester_spin.providers.redtiger.evo_auth import (
    _browser_fetch_json,
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

    def test_json_auth_uses_real_browser_fetch_not_context_request(self) -> None:
        source = inspect.getsource(evo_auth.resolve_json_entry_auth)
        self.assertNotIn("context.request", source)
        self.assertIn("_browser_fetch_json", source)
        fetch_source = inspect.getsource(evo_auth._browser_fetch_json)
        self.assertIn("credentials: 'include'", fetch_source)
        self.assertIn("fetch(url", fetch_source)
        self.assertIn("_abort_browser_fetch(page)", fetch_source)
        abort_source = inspect.getsource(evo_auth._abort_browser_fetch)
        self.assertIn("controller.abort()", abort_source)

    def test_browser_fetch_json_returns_browser_http_result(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.argument = None
                self.launch_script = ""

            def evaluate(self, script, argument=None):
                if argument is not None:
                    self.argument = argument
                    self.launch_script = script
                    return None
                if "return {done:" in script:
                    return {
                        "done": True,
                        "status": 200,
                        "text": '{"location":"/ok"}',
                        "error": "",
                    }
                return None

            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        page = FakePage()
        status, text = _browser_fetch_json(
            page,
            "https://fansite.example/entry?x=opaque",
            3000,
        )
        self.assertEqual(status, 200)
        self.assertEqual(text, '{"location":"/ok"}')
        self.assertEqual(page.argument, {"url": "https://fansite.example/entry?x=opaque"})
        self.assertIn("credentials: 'include'", page.launch_script)

    def test_browser_fetch_json_honors_preexisting_stop_request(self) -> None:
        class NeverCalledPage:
            def evaluate(self, *_args, **_kwargs):
                raise AssertionError("browser fetch should not start after stop")

        stop_event = threading.Event()
        stop_event.set()
        with self.assertRaises(InterruptedError):
            _browser_fetch_json(
                NeverCalledPage(),
                "https://fansite.example/entry?x=opaque",
                3000,
                stop_event=stop_event,
            )


if __name__ == "__main__":
    unittest.main()
