from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.feature_sessions import FEATURE_COMPLETE, FEATURE_INCOMPLETE
from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers.bgaming.feature_sessions import build_bgaming_feature_sessions
from tester_spin.providers.bgaming_farm_adapter import BGamingProvider


def _step(root: Path, number: int, command: str, response: dict) -> None:
    (root / f"step-{number:03d}-request.json").write_text(
        json.dumps({"command": command, "options": {}}), encoding="utf-8"
    )
    (root / f"step-{number:03d}-response.json").write_text(json.dumps(response), encoding="utf-8")
    (root / f"step-{number:03d}-proof.json").write_text(
        json.dumps({"round_id": "r1", "last_action_id": number, "win": number}),
        encoding="utf-8",
    )


def _result(root: Path, *, mode_id: str = "PURCHASE_FREESPIN", kind: str = "PURCHASE", steps: int = 1) -> GameTestResult:
    attempt_dir = root / mode_id / "attempt-001"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    attempt = SpinAttempt(
        number=1,
        ok=True,
        mode_id=mode_id,
        mode_kind=kind,
        terminal=True,
        wire_steps=steps,
        artifact_dir=str(attempt_dir),
    )
    return GameTestResult(
        provider="bgaming",
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


class BGamingFeatureSessionTests(unittest.TestCase):
    def test_ten_freespin_continuations_become_ten_logical_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, steps=11)
            attempt = Path(result.attempts[0].artifact_dir)
            _step(attempt, 1, "spin", {"flow": {"state": "freespin", "round_id": "r1"}})
            for number in range(2, 12):
                _step(
                    attempt,
                    number,
                    "freespin",
                    {"flow": {"state": "closed" if number == 11 else "freespin", "round_id": "r1"}},
                )

            report = build_bgaming_feature_sessions(result)

        session = report["sessions"][0]
        self.assertEqual(session["totals"]["logical_rounds"], 10)
        self.assertEqual(session["totals"]["wire_steps"], 11)
        self.assertEqual(session["state"], FEATURE_COMPLETE)

    def test_unknown_continuation_is_not_silently_counted_as_round(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, steps=2)
            attempt = Path(result.attempts[0].artifact_dir)
            _step(attempt, 1, "spin", {"flow": {"state": "mystery", "round_id": "r1"}})
            _step(attempt, 2, "mystery_new_command", {"flow": {"state": "closed", "round_id": "r1"}})

            report = build_bgaming_feature_sessions(result)

        session = report["sessions"][0]
        self.assertEqual(session["totals"]["logical_rounds"], 0)
        self.assertEqual(session["state"], FEATURE_INCOMPLETE)
        self.assertIn("mystery_new_command", " ".join(session["reasons"]))

    def test_uncovered_flow_choice_blocks_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, steps=2)
            attempt = Path(result.attempts[0].artifact_dir)
            _step(attempt, 1, "spin", {"flow": {"state": "choice", "round_id": "r1"}})
            _step(attempt, 2, "pick", {"flow": {"state": "closed", "round_id": "r1"}})
            result.discovered_modes = [
                {
                    "id": "BGAMING_FLOW_CHOICE_PURCHASE_FREESPIN__PICK__ROOT",
                    "kind": "CHOICE_CONTINUATION",
                    "wire_command": "pick",
                    "coverage_required": True,
                    "branch_signature": "BGAMING:flow-choice:PURCHASE_FREESPIN:pick:[]",
                    "path_prefix": [],
                    "required_options": ["0", "1"],
                    "covered_options": ["0"],
                    "required_samples": 1,
                    "sample_counts": {"0": 1, "1": 0},
                }
            ]

            report = build_bgaming_feature_sessions(result)

        self.assertEqual(report["sessions"][0]["state"], FEATURE_INCOMPLETE)
        self.assertEqual(report["sessions"][0]["choices"][0]["missing_options"], ["1"])

    def test_active_provider_uses_bgaming_normalizer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, mode_id="SPIN", kind="SPIN", steps=2)
            attempt = Path(result.attempts[0].artifact_dir)
            _step(attempt, 1, "spin", {"flow": {"state": "respin", "round_id": "r1"}})
            _step(attempt, 2, "respin", {"flow": {"state": "closed", "round_id": "r1"}})
            provider = BGamingProvider(root / "data")

            report = provider.build_feature_sessions(result)

        self.assertEqual(report["authority"], "bgaming-flow-state+continuation-wire+choice-graph")
        self.assertEqual(report["sessions"][0]["trigger"], "NATURAL")


if __name__ == "__main__":
    unittest.main()
