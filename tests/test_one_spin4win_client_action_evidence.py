from __future__ import annotations

import hashlib
import unittest

from tester_spin.providers.one_spin4win_client_evidence import (
    build_client_action_evidence,
    extract_client_action_evidence,
)


class OneSpin4WinClientActionEvidenceTests(unittest.TestCase):
    def test_extracts_controller_methods_without_assigning_semantics(self) -> None:
        source = '''
            this.gameController.connect("Game","user","debug","","01","","");
            this.gameController.playGame(lines, betIndex, 0);
            if (canDouble) this.gameController.gamble(value);
            otherController.playGame();
            this.gameController.playGame(lines, betIndex, 0);
        '''

        evidence = extract_client_action_evidence(source)

        self.assertEqual(
            evidence["game_controller_methods"],
            ["connect", "gamble", "playGame"],
        )
        self.assertEqual(evidence["method_call_counts"]["playGame"], 2)
        self.assertEqual(evidence["method_call_counts"]["connect"], 1)
        self.assertEqual(evidence["method_call_counts"]["gamble"], 1)

    def test_empty_source_produces_empty_evidence(self) -> None:
        self.assertEqual(
            extract_client_action_evidence(""),
            {
                "game_controller_methods": [],
                "method_call_counts": {},
            },
        )

    def test_build_evidence_hashes_sources_and_aggregates_methods(self) -> None:
        first = "this.gameController.connect(); this.gameController.playGame();"
        second = "this.gameController.playGame(); this.gameController.gamble();"

        evidence = build_client_action_evidence(
            [("https://example/a.js", first), ("https://example/b.js", second)]
        )

        self.assertEqual(
            evidence["aggregate"]["game_controller_methods"],
            ["connect", "gamble", "playGame"],
        )
        self.assertEqual(
            evidence["aggregate"]["method_call_counts"],
            {"connect": 1, "gamble": 1, "playGame": 2},
        )
        self.assertEqual(
            evidence["scripts"][0]["sha256"],
            hashlib.sha256(first.encode("utf-8")).hexdigest(),
        )
        self.assertNotIn("source", evidence["scripts"][0])


if __name__ == "__main__":
    unittest.main()
