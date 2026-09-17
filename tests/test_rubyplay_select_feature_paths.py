from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.feature_sessions import FEATURE_COMPLETE, FEATURE_INCOMPLETE
from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers.rubyplay.feature_sessions import build_rubyplay_feature_sessions


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _result(root: Path, second_complete: bool) -> GameTestResult:
    attempt_dir = root / "PURCHASE_SELECT" / "attempt-00001"
    attempt_dir.mkdir(parents=True)
    _write(attempt_dir / "request.json", {"action": "buy_feature", "buy_feature_type": "select"})
    _write(attempt_dir / "response.json", {"data": {"next_action": "select"}})
    _write(attempt_dir / "step-002-request.json", {"action": "select", "index": 1})
    _write(attempt_dir / "step-002-response.json", {"data": {"next_action": "freespin"}})
    _write(attempt_dir / "step-003-request.json", {"action": "freespin"})
    _write(attempt_dir / "step-003-response.json", {"data": {"next_action": "select"}})
    _write(attempt_dir / "step-004-request.json", {"action": "select", "index": 0})
    _write(attempt_dir / "step-004-response.json", {"data": {"next_action": "spin"}})

    return GameTestResult(
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
                mode_id="PURCHASE_SELECT",
                mode_kind="PURCHASE",
                terminal=True,
                na="spin",
                wire_steps=4,
                artifact_dir=str(attempt_dir),
            )
        ],
        discovered_modes=[
            {
                "id": "PURCHASE_SELECT__SELECT_INDEX_DOMAIN",
                "kind": "INDEXED_CHOICE",
                "parent": "PURCHASE_SELECT",
                "prefix": [],
                "wire_command": "select",
                "coverage_required": True,
                "domain_authority": "isolated-live-server-rejection-window",
                "boundary_index": 2,
                "boundary_confirmations": 2,
                "rejection_span": 2,
                "required_options": ["0", "1"],
                "covered_options": ["0", "1"],
                "required_samples": 1,
                "sample_counts": {"0": 1, "1": 1},
            },
            {
                "id": "PURCHASE_SELECT__SELECT_INDEX_DOMAIN__PREFIX_X",
                "kind": "INDEXED_CHOICE",
                "parent": "PURCHASE_SELECT",
                "prefix": ["select=1"],
                "wire_command": "select",
                "coverage_required": True,
                "domain_authority": "isolated-live-server-rejection-window",
                "boundary_index": 1,
                "boundary_confirmations": 2,
                "rejection_span": 2,
                "required_options": ["0"],
                "covered_options": ["0"] if second_complete else [],
                "required_samples": 1,
                "sample_counts": {"0": 1 if second_complete else 0},
            },
        ],
    )


class RubyPlaySelectFeaturePathTests(unittest.TestCase):
    def test_two_select_prompts_bind_to_distinct_prefix_domains(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = _result(Path(temp), second_complete=True)
            report = build_rubyplay_feature_sessions(result)

        session = report["sessions"][0]
        self.assertEqual(session["state"], FEATURE_COMPLETE)
        self.assertEqual(len(session["choices"]), 2)
        self.assertEqual(session["choices"][0]["prefix"], [])
        self.assertEqual(session["choices"][1]["prefix"], ["select=1"])
        self.assertTrue(session["choices"][0]["complete"])
        self.assertTrue(session["choices"][1]["complete"])

    def test_incomplete_second_select_domain_keeps_session_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = _result(Path(temp), second_complete=False)
            report = build_rubyplay_feature_sessions(result)

        session = report["sessions"][0]
        self.assertEqual(session["state"], FEATURE_INCOMPLETE)
        self.assertTrue(session["choices"][0]["complete"])
        self.assertFalse(session["choices"][1]["complete"])
        self.assertEqual(session["choices"][1]["missing_options"], ["0"])


if __name__ == "__main__":
    unittest.main()
