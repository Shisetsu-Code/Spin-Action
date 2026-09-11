from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from tester_spin.providers.redtiger import demo_auth
from tester_spin.providers.redtiger.demo_auth import (
    api_key_from_headers,
    demo_page_url,
    post_demo_token,
)


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, statuses: list[int] | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict, float, str]] = []
        self._statuses = list(statuses or [401, 200])

    def post(self, url: str, *, json: dict, timeout: float):
        self.calls.append((url, json, timeout, self.headers.get("x-api-key", "")))
        return _Response(self._statuses.pop(0))


class RedTigerDemoAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        with demo_auth._API_KEY_CACHE_LOCK:
            demo_auth._API_KEY_CACHE.clear()

    def test_api_key_header_lookup_is_case_insensitive(self) -> None:
        self.assertEqual(
            api_key_from_headers({"X-Api-Key": "dynamic-key"}),
            "dynamic-key",
        )

    def test_demo_route_is_exact_public_route_and_uses_table_id(self) -> None:
        self.assertEqual(
            demo_page_url(
                "https://redtiger.example/games/a-public-slug",
                "opaque-table-7xq",
            ),
            "https://redtiger.example/demo/opaque-table-7xq?showNavbar=true",
        )

    def test_unauthorized_demo_token_retries_with_observed_frontend_key(self) -> None:
        session = _Session()
        progress: list[str] = []
        payload = {
            "demo": {"language": "en-GB", "currency": "VC0"},
            "game": {"tableId": "opaque-table"},
        }

        with patch(
            "tester_spin.providers.redtiger.demo_auth.discover_demo_api_key",
            return_value="observed-at-runtime",
        ) as discover:
            response = post_demo_token(
                session,  # type: ignore[arg-type]
                "https://demo.example/api/v1/oss/token/demo",
                "https://redtiger.example/games/opaque-game",
                payload,
                timeout_s=7.0,
                progress=progress.append,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0][3], "")
        self.assertEqual(session.calls[1][3], "observed-at-runtime")
        discover.assert_called_once_with(
            "https://redtiger.example/games/opaque-game",
            "https://demo.example/api/v1/oss/token/demo",
            "opaque-table",
            timeout_s=15.0,
        )
        self.assertTrue(any("x-api-key" in message for message in progress))

    def test_observed_key_is_reused_from_memory_across_games(self) -> None:
        first = _Session([401, 200])
        with patch(
            "tester_spin.providers.redtiger.demo_auth.discover_demo_api_key",
            return_value="observed-at-runtime",
        ):
            post_demo_token(
                first,  # type: ignore[arg-type]
                "https://demo.example/api/v1/oss/token/demo",
                "https://redtiger.example/games/first",
                {"game": {"tableId": "table-first"}},
                timeout_s=7.0,
            )

        second = _Session([200])
        with patch(
            "tester_spin.providers.redtiger.demo_auth.discover_demo_api_key",
            side_effect=AssertionError("cached key should avoid browser discovery"),
        ):
            response = post_demo_token(
                second,  # type: ignore[arg-type]
                "https://demo.example/api/v1/oss/token/demo",
                "https://redtiger.example/games/second",
                {"game": {"tableId": "table-second"}},
                timeout_s=7.0,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(second.calls), 1)
        self.assertEqual(second.calls[0][3], "observed-at-runtime")

    def test_stale_cached_key_is_discarded_and_rediscovered(self) -> None:
        demo_auth._remember_api_key(
            "https://redtiger.example/games/first",
            "https://demo.example/api/v1/oss/token/demo",
            "stale-key",
        )
        session = _Session([401, 200])
        with patch(
            "tester_spin.providers.redtiger.demo_auth.discover_demo_api_key",
            return_value="fresh-key",
        ):
            response = post_demo_token(
                session,  # type: ignore[arg-type]
                "https://demo.example/api/v1/oss/token/demo",
                "https://redtiger.example/games/second",
                {"game": {"tableId": "table-second"}},
                timeout_s=7.0,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(session.calls[0][3], "stale-key")
        self.assertEqual(session.calls[1][3], "fresh-key")


if __name__ == "__main__":
    unittest.main()
