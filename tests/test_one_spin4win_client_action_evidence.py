from __future__ import annotations

import unittest

from tester_spin.providers.one_spin4win import _extract_client_action_evidence


class OneSpin4WinClientActionEvidenceTests(unittest.TestCase):
    def test_extracts_controller_methods_without_assigning_semantics(self) -> None:
        source = '''
            this.gameController.connect("Game","user","debug","","01","","");
            this.gameController.playGame(lines, betIndex, 0);
            if (canDouble) this.gameController.gamble(value);
            otherController.playGame();
            this.gameController.playGame(lines, betIndex, 0);
        '''

        evidence = _extract_client_action_evidence(source)

        self.assertEqual(
            evidence["game_controller_methods"],
            ["connect", "gamble", "playGame"],
        )
        self.assertEqual(evidence["method_call_counts"]["playGame"], 2)
        self.assertEqual(evidence["method_call_counts"]["connect"], 1)
        self.assertEqual(evidence["method_call_counts"]["gamble"], 1)

    def test_empty_source_produces_empty_evidence(self) -> None:
        self.assertEqual(
            _extract_client_action_evidence(""),
            {
                "game_controller_methods": [],
                "method_call_counts": {},
            },
        )


if __name__ == "__main__":
    unittest.main()
