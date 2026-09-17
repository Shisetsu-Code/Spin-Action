from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.feature_sessions import FEATURE_COMPLETE, FEATURE_INCOMPLETE
from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers.one_spin4win_feature_sessions import build_one_spin4win_feature_sessions
from tester_spin.providers.one_spin4win_farm_adapter import OneSpin4WinProvider


def _frame(direction: str, payload: dict, *, classification: str = "") -> dict:
    row = {
        "direction": direction,
        "payload": {"kind": "text", "text": "A/u2" + json.dumps(payload)},
    }
    if classification:
        row["classification"] = classification
    return row


def _result(root: Path, frames: list[dict], *, terminal: bool = True) -> GameTestResult:
    attempt_dir = root / "SPIN" / "attempt-00001"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "frames": frames,
        "terminal": terminal,
        "wire_steps": sum(1 for frame in frames if frame.get("direction") == "sent"),
        "final_result": {"type": 3, "st": 0 if terminal else 99},
    }
    (attempt_dir / "ws-attempt.json").write_text(json.dumps(artifact), encoding="utf-8")
    attempt = SpinAttempt(
        number=1,
        ok=terminal,
        mode_id="SPIN",
        mode_kind="SPIN",
        terminal=terminal,
        wire_steps=artifact["wire_steps"],
        artifact_dir=str(attempt_dir),
    )
    return GameTestResult(
        provider="1spin4win",
        slug="synthetic",
        game_name="Synthetic",
        game_url="https://example.invalid/game",
        requested_spins=1,
        successful_spins=int(terminal),
        failed_spins=int(not terminal),
        status="OK" if terminal else "PARCIAL",
        run_dir=str(root),
        attempts=[attempt],
    )


class OneSpin4WinFeatureSessionTests(unittest.TestCase):
    def test_root_plus_ten_continuation_pairs_becomes_ten_feature_rounds(self) -> None:
        frames = [
            _frame("sent", {"type": 1}, classification="spin"),
            _frame("received", {"type": 3, "st": 5}),
        ]
        for index in range(10):
            frames.append(_frame("sent", {"type": 1}, classification="continuation"))
            frames.append(_frame("received", {"type": 3, "st": 0 if index == 9 else 5}))

        with tempfile.TemporaryDirectory() as temp:
            result = _result(Path(temp), frames)
            report = build_one_spin4win_feature_sessions(result)

        self.assertEqual(report["session_count"], 1)
        session = report["sessions"][0]
        self.assertEqual(session["trigger"], "NATURAL")
        self.assertEqual(session["parent_mode"], "SPIN")
        self.assertEqual(session["totals"]["logical_rounds"], 10)
        self.assertEqual(session["state"], FEATURE_COMPLETE)

    def test_unknown_result_state_keeps_feature_incomplete(self) -> None:
        frames = [
            _frame("sent", {"type": 1}, classification="spin"),
            _frame("received", {"type": 3, "st": 5}),
            _frame("sent", {"type": 1}, classification="continuation"),
            _frame("received", {"type": 3, "st": 99}),
        ]
        with tempfile.TemporaryDirectory() as temp:
            result = _result(Path(temp), frames, terminal=False)
            report = build_one_spin4win_feature_sessions(result)

        self.assertEqual(report["sessions"][0]["state"], FEATURE_INCOMPLETE)
        self.assertIn("99", " ".join(report["sessions"][0]["reasons"]))

    def test_base_spin_without_active_state_creates_no_feature_session(self) -> None:
        frames = [
            _frame("sent", {"type": 1}, classification="spin"),
            _frame("received", {"type": 3, "st": 0}),
        ]
        with tempfile.TemporaryDirectory() as temp:
            result = _result(Path(temp), frames)
            report = build_one_spin4win_feature_sessions(result)
        self.assertEqual(report["session_count"], 0)
        self.assertTrue(report["complete"])

    def test_active_provider_build_hook_uses_d1_normalizer(self) -> None:
        frames = [
            _frame("sent", {"type": 1}, classification="spin"),
            _frame("received", {"type": 3, "st": 5}),
            _frame("sent", {"type": 1}, classification="continuation"),
            _frame("received", {"type": 3, "st": 0}),
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, frames)
            provider = OneSpin4WinProvider(root / "data")
            report = provider.build_feature_sessions(result)
        self.assertEqual(report["authority"], "d1-websocket-type1-type3-state-machine")
        self.assertEqual(report["sessions"][0]["totals"]["logical_rounds"], 1)


if __name__ == "__main__":
    unittest.main()
