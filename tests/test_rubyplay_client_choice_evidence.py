from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.providers.rubyplay.choice_client_evidence import (
    build_choice_client_evidence,
    write_choice_client_evidence,
)


class RubyPlayChoiceClientEvidenceTests(unittest.TestCase):
    def test_strong_choice_handler_contexts_are_persisted_compactly(self) -> None:
        scripts = [
            (
                "https://cdn.example/game.js",
                (
                    "A" * 900
                    + "class PickMessageHandler{handle(index){return index<8}};"
                    + "B" * 900
                    + "class SelectMessageHandler{handle(index){return index>=0&&index<3}};"
                    + "C" * 900
                ),
            )
        ]

        evidence = build_choice_client_evidence(scripts)

        self.assertEqual(evidence["schema"], "tester-spin/rubyplay-choice-client-evidence/v1")
        self.assertEqual(len(evidence["scripts"]), 1)
        row = evidence["scripts"][0]
        self.assertEqual(row["url"], "https://cdn.example/game.js")
        self.assertEqual(len(row["sha256"]), 64)
        markers = {hit["marker"] for hit in row["hits"]}
        self.assertEqual(markers, {"PickMessageHandler", "SelectMessageHandler"})
        self.assertTrue(all(len(hit["excerpt"]) <= 1400 for hit in row["hits"]))
        self.assertTrue(all(len(hit["excerpt_sha256"]) == 64 for hit in row["hits"]))

    def test_generic_action_literal_without_index_context_is_not_promoted_as_handler_evidence(self) -> None:
        evidence = build_choice_client_evidence(
            [
                (
                    "https://cdn.example/game.js",
                    "const labels=['pick','select']; function render(){return labels.join(',')}",
                )
            ]
        )

        self.assertEqual(evidence["scripts"][0]["hits"], [])

    def test_writer_creates_machine_readable_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = write_choice_client_evidence(
                root,
                [
                    (
                        "https://cdn.example/game.js",
                        "class PickMessageHandler{handle(index){return index<4}}",
                    )
                ],
            )
            self.assertEqual(path, root / "client-choice-evidence.json")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["scripts"][0]["hits"][0]["marker"], "PickMessageHandler")


if __name__ == "__main__":
    unittest.main()
