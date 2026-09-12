from __future__ import annotations

import unittest

from tester_spin.providers.redtiger.result_tree import observed_modes, result_nodes
from tester_spin.providers.redtiger.runtime import validate_spin_response


class RedTigerStateNodeWithoutSpinModeTests(unittest.TestCase):
    @staticmethod
    def _captured_shape() -> dict:
        return {
            "success": True,
            "result": {
                "game": {
                    "win": {"lines": "0.00", "total": "0.00"},
                    "stake": "2.00",
                    "multiplier": 1,
                    "winLines": [],
                    "nearMiss": [],
                    "reelsBuffer": [[[1, 2, 3]]],
                    "features": [],
                    "gameMode": 0,
                    "hasState": False,
                }
            },
        }

    def test_terminal_spin_without_spin_mode_is_valid(self) -> None:
        payload = self._captured_shape()
        valid, warnings = validate_spin_response(payload)
        self.assertTrue(valid)
        self.assertEqual(warnings, [])

    def test_state_node_is_preserved_without_inventing_spin_mode(self) -> None:
        game = self._captured_shape()["result"]["game"]
        nodes = result_nodes(game)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].spin_mode, "")
        self.assertEqual(nodes[0].game_mode, 0)
        self.assertIs(nodes[0].has_state, False)
        self.assertEqual(observed_modes(game), [])

    def test_game_mode_alone_is_not_enough_to_create_a_state_node(self) -> None:
        self.assertEqual(result_nodes({"gameMode": 0, "hasState": False}), [])


if __name__ == "__main__":
    unittest.main()
