from __future__ import annotations

import unittest

from tester_spin.providers.rubyplay.farm_adapter import _RateLimitedRubyPlaySession


class _Inner:
    def __init__(self) -> None:
        self.headers = {"X": "1"}
        self.calls = []
        self.closed = False

    def get(self, *args, **kwargs):
        self.calls.append(("get", args, kwargs))
        return "GET"

    def post(self, *args, **kwargs):
        self.calls.append(("post", args, kwargs))
        return "POST"

    def close(self):
        self.closed = True


class _Provider:
    def __init__(self, allowed=True) -> None:
        self.allowed = allowed
        self.slots = 0

    def acquire_provider_request_slot(self):
        self.slots += 1
        return self.allowed


class RubyPlayRateLimitTests(unittest.TestCase):
    def test_get_and_post_share_provider_request_budget(self) -> None:
        provider = _Provider()
        inner = _Inner()
        session = _RateLimitedRubyPlaySession(provider, inner)

        self.assertEqual(session.get("https://example.invalid/a", timeout=1), "GET")
        self.assertEqual(session.post("https://example.invalid/b", json={}), "POST")

        self.assertEqual(provider.slots, 2)
        self.assertEqual([row[0] for row in inner.calls], ["get", "post"])
        self.assertIs(session.headers, inner.headers)

    def test_denied_slot_prevents_network_call(self) -> None:
        provider = _Provider(allowed=False)
        inner = _Inner()
        session = _RateLimitedRubyPlaySession(provider, inner)

        with self.assertRaises(InterruptedError):
            session.post("https://example.invalid/b", json={})

        self.assertEqual(provider.slots, 1)
        self.assertEqual(inner.calls, [])

    def test_close_delegates_to_wrapped_session(self) -> None:
        provider = _Provider()
        inner = _Inner()
        session = _RateLimitedRubyPlaySession(provider, inner)
        session.close()
        self.assertTrue(inner.closed)


if __name__ == "__main__":
    unittest.main()
