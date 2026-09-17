from __future__ import annotations

import unittest

import requests

from tester_spin.providers.rubyplay.choice_domains import classify_probe_failure


class RubyPlayProtocolRejectionTests(unittest.TestCase):
    def test_provider_index_error_is_semantic_rejection(self) -> None:
        outcome = classify_probe_failure(
            ValueError("RubyPlay select: status='error'."),
            last_payload={
                "status": "error",
                "topic": "gameserver/select",
                "error": "invalid index",
            },
            action="select",
        )
        self.assertEqual(outcome["outcome"], "SEMANTIC_REJECTION")
        self.assertEqual(outcome["provider_status"], "error")
        self.assertEqual(outcome["provider_error"], "invalid index")

    def test_unrelated_provider_error_cannot_define_index_boundary(self) -> None:
        outcome = classify_probe_failure(
            ValueError("RubyPlay select: status='error'."),
            last_payload={
                "status": "error",
                "topic": "gameserver/select",
                "error": "session expired",
            },
            action="select",
        )
        self.assertEqual(outcome["outcome"], "PROTOCOL_ERROR")

    def test_wrong_topic_cannot_define_index_boundary(self) -> None:
        outcome = classify_probe_failure(
            ValueError("RubyPlay select: status='error'."),
            last_payload={
                "status": "error",
                "topic": "gameserver/spin",
                "error": "invalid index",
            },
            action="select",
        )
        self.assertEqual(outcome["outcome"], "PROTOCOL_ERROR")

    def test_transport_error_is_never_semantic_boundary(self) -> None:
        outcome = classify_probe_failure(
            requests.Timeout("timed out"),
            last_payload=None,
            action="select",
        )
        self.assertEqual(outcome["outcome"], "TRANSPORT_ERROR")

    def test_malformed_protocol_error_is_not_semantic_boundary(self) -> None:
        outcome = classify_probe_failure(
            ValueError("RubyPlay select: data.next_action vacío."),
            last_payload={"status": "ok", "data": {}},
            action="select",
        )
        self.assertEqual(outcome["outcome"], "PROTOCOL_ERROR")


if __name__ == "__main__":
    unittest.main()
