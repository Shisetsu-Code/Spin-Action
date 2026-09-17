from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.feature_sessions import FEATURE_COMPLETE
from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers.rubyplay.feature_sessions import build_rubyplay_feature_sessions


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class RubyPlayMultiplePickDomainTests(unittest.TestCase):
    def test_pick_chain_then_freespin_then_new_pick_creates_two_choice_domains(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt_dir = root / "PURCHASE_PICK" / "attempt-00001"
            attempt_dir.mkdir(parents=True)
            _write(attempt_dir / "request.json", {"action": "buy_feature", "buy_feature_type": "pick"})
            _write(attempt_dir / "response.json", {"data": {"next_action": "pick"}})
            _write(attempt_dir / "step-002-request.json", {"action": "pick", "index": 0})
            _write(attempt_dir / "step-002-response.json", {"data": {"next_action": "pick"}})
            _write(attempt_dir / "step-003-request.json", {"action": "pick", "index": 1})
            _write(attempt_dir / "step-003-response.json", {"data": {"next_action": "freespin"}})
            _write(attempt_dir / "step-004-request.json", {"action": "freespin"})
            _write(attempt_dir / "step-004-response.json", {"data": {"next_action": "pick"}})
            _write(attempt_dir / "step-005-request.json", {"action": "pick", "index": 0})
            _write(attempt_dir / "step-005-response.json", {"data": {"next_action": "spin"}})

            result = GameTestResult(
                provider="rubyplay",
                slug="g",
                game_name="G",
                game_url="https://example.invalid/g",
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="OK",
                run_dir=str(root),
                attempts=[
                    SpinAttempt(
                        number=1,
                        ok=True,
                        mode_id="PURCHASE_PICK",
                        mode_kind="PURCHASE",
                        terminal=True,
                        na="spin",
                        wire_steps=5,
                        artifact_dir=str(attempt_dir),
                    )
                ],
                discovered_modes=[
                    {
                        "id": "PURCHASE_PICK__PICK_INDEX_DOMAIN",
                        "kind": "INDEXED_CHOICE",
                        "parent": "PURCHASE_PICK",
                        "prefix": [],
                        "wire_command": "pick",
                        "coverage_required": True,
                        "required_options": ["0", "1"],
                        "covered_options": ["0", "1"],
                        "required_samples": 1,
                        "sample_counts": {"0": 1, "1": 1},
                    },
                    {
                        "id": "PURCHASE_PICK__PICK_INDEX_DOMAIN__PREFIX_SECOND",
                        "kind": "INDEXED_CHOICE",
                        "parent": "PURCHASE_PICK",
                        "prefix": ["pick=0", "pick=1"],
                        "wire_command": "pick",
                        "coverage_required": True,
                        "required_options": ["0"],
                        "covered_options": ["0"],
                        "required_samples": 1,
                        "sample_counts": {"0": 1},
                    },
                ],
            )

            report = build_rubyplay_feature_sessions(result)

        session = report["sessions"][0]
        self.assertEqual(session["state"], FEATURE_COMPLETE)
        self.assertEqual(session["totals"]["choices"], 2)
        self.assertEqual(
            [choice["prefix"] for choice in session["choices"]],
            [[], ["pick=0", "pick=1"]],
        )
        self.assertEqual(
            [row["selected"] for row in session["transitions"] if row.get("provider_action") == "pick"],
            ["0", "1", "0"],
        )


if __name__ == "__main__":
    unittest.main()
