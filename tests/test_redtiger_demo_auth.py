from __future__ import annotations

import unittest
from unittest.mock import patch

from tester_spin.providers.redtiger.demo_auth import (
    api_key_from_headers,
    post_demo_token,
)


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict, float, str]] = []
        self._statuses = [401, 200]

    def post(self, url: str, *, json: dict, timeout: float):
        self.calls.append((url, json, timeout, self.headers.get("x-api-key", "")))
        return _Response(self._statuses.pop(0))


class RedTigerDemoAuthTests(unittest.TestCase):
    def test_api_key_header_lookup_is_case_insensitive(self) -> None:
        self.assertEqual(
            api_key_from_headers({"X-Api-Key": "dynamic-key"}),
            "dynamic-key",
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
            timeout_s=15.0,
        )
        self.assertTrue(any("x-api-key" in message for message in progress))


if __name__ == "__main__":
    unittest.main()
