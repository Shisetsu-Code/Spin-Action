from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers import OneSpin4WinProvider
from tester_spin.providers.one_spin4win_exhaustive import apply_d1_path_audit


class D1TerminalStateAuditTests(unittest.TestCase):
    def test_state_3_is_terminal_only_when_terminal_artifact_names_it_as_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = root / "SPIN" / "attempt-001"
            attempt.mkdir(parents=True)
            final_result = {"type": "3", "st": 3, "l": 20, "b3": 1}
            (attempt / "ws-attempt.json").write_text(
                json.dumps(
                    {
                        "terminal": True,
                        "final_result": final_result,
                        "frames": [
                            {
                                "direction": "received",
                                "payload": {
                                    "kind": "text",
                                    "text": json.dumps(final_result),
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = GameTestResult(
                provider="1spin4win",
                slug="synthetic",
                game_name="Synthetic",
                game_url="https://example.invalid/game",
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="OK",
                run_dir=str(root),
                attempts=[
                    SpinAttempt(
                        number=1,
                        ok=True,
                        mode_id="SPIN",
                        terminal=True,
                        artifact_dir=str(attempt),
                    )
                ],
            )
            provider = OneSpin4WinProvider(root / "data")
            apply_d1_path_audit(provider, result, progress=lambda _message: None)
            self.assertEqual(result.status, "OK")
            self.assertNotIn(
                "D1_UNKNOWN_RESULT_STATES",
                [mode.get("id") for mode in result.discovered_modes],
            )


if __name__ == "__main__":
    unittest.main()
