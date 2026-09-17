from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.feature_sessions import FEATURE_INCOMPLETE
from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers.belatra_feature_sessions import build_belatra_feature_sessions
from tester_spin.providers.belatra_farm_adapter import BelatraProvider


def _result(root: Path) -> GameTestResult:
    attempt_dir = root / "SPIN" / "attempt-001"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    attempt = SpinAttempt(
        number=1,
        ok=True,
        mode_id="SPIN",
        mode_kind="SPIN",
        terminal=True,
        wire_steps=2,
        artifact_dir=str(attempt_dir),
    )
    return GameTestResult(
        provider="belatra",
        slug="synthetic",
        game_name="Synthetic",
        game_url="https://example.invalid/game",
        requested_spins=1,
        successful_spins=1,
        failed_spins=0,
        status="OK",
        run_dir=str(root),
        attempts=[attempt],
    )


def _write(root: Path, name: str, gs: dict) -> None:
    (root / name).write_text(json.dumps({"gs": gs}), encoding="utf-8")


class BelatraFeatureSessionTests(unittest.TestCase):
    def test_base_paid_finish_path_does_not_invent_feature_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root)
            attempt = Path(result.attempts[0].artifact_dir)
            _write(attempt, "start.response.json", {"phaseCur": "spin", "phaseNext": "toPaid", "historyId": 1})
            _write(attempt, "finish.response.json", {"phaseCur": "finished", "phaseNext": "toIdle"})
            report = build_belatra_feature_sessions(result)
        self.assertEqual(report["session_count"], 0)

    def test_double_dialog_with_only_decline_covered_is_incomplete_choice_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root)
            attempt = Path(result.attempts[0].artifact_dir)
            _write(attempt, "start.response.json", {"phaseCur": "spin", "phaseNext": "toDoubleDialog", "historyId": 1})
            _write(attempt, "finish.response.json", {"phaseCur": "finished", "phaseNext": "toIdle"})
            result.discovered_modes = [
                {
                    "id": "BELATRA_DOUBLE_DIALOG",
                    "kind": "CHOICE_BRANCH",
                    "wire_command": "double-dialog",
                    "coverage_required": True,
                    "required_options": ["DECLINE", "GAMBLE"],
                    "covered_options": ["DECLINE"],
                    "branch_signature": "BELATRA:toDoubleDialog",
                }
            ]
            report = build_belatra_feature_sessions(result)
        session = report["sessions"][0]
        self.assertEqual(session["state"], FEATURE_INCOMPLETE)
        self.assertEqual(session["choices"][0]["missing_options"], ["GAMBLE"])

    def test_unknown_nonterminal_phase_is_preserved_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root)
            result.attempts[0].ok = False
            result.attempts[0].terminal = False
            attempt = Path(result.attempts[0].artifact_dir)
            _write(attempt, "start.response.json", {"phaseCur": "spin", "phaseNext": "toMysteryFeature", "historyId": 1})
            report = build_belatra_feature_sessions(result)
        session = report["sessions"][0]
        self.assertEqual(session["state"], FEATURE_INCOMPLETE)
        self.assertIn("toMysteryFeature", " ".join(session["reasons"]))

    def test_active_provider_uses_belatra_normalizer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root)
            attempt = Path(result.attempts[0].artifact_dir)
            _write(attempt, "start.response.json", {"phaseCur": "spin", "phaseNext": "toDoubleDialog", "historyId": 1})
            _write(attempt, "finish.response.json", {"phaseCur": "finished", "phaseNext": "toIdle"})
            result.discovered_modes = [
                {
                    "id": "BELATRA_DOUBLE_DIALOG",
                    "kind": "CHOICE_BRANCH",
                    "coverage_required": True,
                    "required_options": ["DECLINE", "GAMBLE"],
                    "covered_options": ["DECLINE"],
                }
            ]
            provider = BelatraProvider(root / "data")
            report = provider.build_feature_sessions(result)
        self.assertEqual(report["authority"], "belatra-phase-state+choice-coverage")
        self.assertEqual(report["session_count"], 1)


if __name__ == "__main__":
    unittest.main()
