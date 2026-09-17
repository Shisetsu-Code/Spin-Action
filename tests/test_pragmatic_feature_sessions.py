from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from tester_spin.feature_sessions import FEATURE_COMPLETE, FEATURE_INCOMPLETE
from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers.pragmatic_feature_sessions import build_pragmatic_feature_sessions
from tester_spin.providers.pragmatic_farm_adapter import PragmaticProvider


def _wire(root: Path, step: int, label: str, request: dict, response: dict) -> None:
    prefix = f"step-{step:03d}-{label}"
    (root / f"{prefix}.request.txt").write_text(urlencode(request), encoding="utf-8")
    (root / f"{prefix}.response.json").write_text(json.dumps(response), encoding="utf-8")


def _result(root: Path, *, mode_id: str = "PURCHASE_1", kind: str = "PURCHASE", steps: int = 1) -> GameTestResult:
    attempt_dir = root / mode_id / "attempt-0001"
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
        provider="pragmatic",
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


class PragmaticFeatureSessionTests(unittest.TestCase):
    def test_ten_continuation_spins_are_ten_rounds_not_all_wire_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, steps=12)
            attempt = Path(result.attempts[0].artifact_dir)
            _wire(attempt, 0, "entry", {"action": "doSpin", "pur": "1"}, {"na": "s", "fs": "1"})
            for step in range(1, 11):
                _wire(
                    attempt,
                    step,
                    "continuation-spin",
                    {"action": "doSpin"},
                    {"na": "s" if step < 10 else "c", "fs": "1" if step < 10 else ""},
                )
            _wire(attempt, 11, "collect", {"action": "doCollect"}, {"na": ""})

            report = build_pragmatic_feature_sessions(result)

        session = report["sessions"][0]
        self.assertEqual(session["trigger"], "PURCHASE")
        self.assertEqual(session["totals"]["logical_rounds"], 10)
        self.assertEqual(session["totals"]["wire_steps"], 12)
        self.assertEqual(session["state"], FEATURE_COMPLETE)

    def test_unknown_entry_na_is_preserved_as_incomplete_feature(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, mode_id="PURCHASE_X", kind="PURCHASE", steps=1)
            attempt = Path(result.attempts[0].artifact_dir)
            _wire(attempt, 0, "entry", {"action": "doSpin", "pur": "7"}, {"na": "xbonus"})
            result.attempts[0].terminal = False
            result.attempts[0].warning = "estado de continuación no automatizado: na='xbonus'"

            report = build_pragmatic_feature_sessions(result)

        self.assertEqual(report["session_count"], 1)
        session = report["sessions"][0]
        self.assertEqual(session["state"], FEATURE_INCOMPLETE)
        self.assertEqual(session["entry"]["na"], "xbonus")
        self.assertIn("xbonus", " ".join(session["reasons"]))

    def test_missing_fso_sibling_keeps_feature_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, steps=2)
            attempt = Path(result.attempts[0].artifact_dir)
            _wire(attempt, 0, "entry", {"action": "doSpin", "pur": "1"}, {"na": "fso", "fs_opt": "1~2"})
            _wire(attempt, 1, "fs-option-0", {"action": "doFSOption", "ind": "0"}, {"na": ""})
            result.discovered_modes = [
                {
                    "id": "PURCHASE_1__FSO_BRANCH_ROOT",
                    "kind": "FSO_BRANCH",
                    "parent": "PURCHASE_1",
                    "prefix": [],
                    "wire_command": "doFSOption",
                    "coverage_required": True,
                    "required_options": ["0", "1"],
                    "covered_options": ["0"],
                    "required_samples": 1,
                    "sample_counts": {"0": 1, "1": 0},
                }
            ]

            report = build_pragmatic_feature_sessions(result)

        self.assertEqual(report["sessions"][0]["state"], FEATURE_INCOMPLETE)
        self.assertEqual(report["sessions"][0]["choices"][0]["missing_options"], ["1"])

    def test_closed_fso_domain_completes_feature_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, steps=2)
            attempt = Path(result.attempts[0].artifact_dir)
            _wire(attempt, 0, "entry", {"action": "doSpin", "pur": "1"}, {"na": "fso", "fs_opt": "1~2"})
            _wire(attempt, 1, "fs-option-0", {"action": "doFSOption", "ind": "0"}, {"na": ""})
            result.discovered_modes = [
                {
                    "id": "PURCHASE_1__FSO_BRANCH_ROOT",
                    "kind": "FSO_BRANCH",
                    "parent": "PURCHASE_1",
                    "prefix": [],
                    "wire_command": "doFSOption",
                    "coverage_required": True,
                    "required_options": ["0", "1"],
                    "covered_options": ["0", "1"],
                    "required_samples": 1,
                    "sample_counts": {"0": 1, "1": 1},
                }
            ]

            report = build_pragmatic_feature_sessions(result)

        self.assertEqual(report["sessions"][0]["state"], FEATURE_COMPLETE)

    def test_active_provider_uses_pragmatic_normalizer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, mode_id="SPIN", kind="SPIN", steps=2)
            attempt = Path(result.attempts[0].artifact_dir)
            _wire(attempt, 0, "entry", {"action": "doSpin"}, {"na": "s", "fs": "1"})
            _wire(attempt, 1, "continuation-spin", {"action": "doSpin"}, {"na": ""})
            provider = PragmaticProvider(root / "data")

            report = provider.build_feature_sessions(result)

        self.assertEqual(report["authority"], "pragmatic-gameService-feature-state+FSO-branches")
        self.assertEqual(report["sessions"][0]["totals"]["logical_rounds"], 1)


if __name__ == "__main__":
    unittest.main()
