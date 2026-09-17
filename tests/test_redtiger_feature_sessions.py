from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.feature_sessions import FEATURE_COMPLETE, FEATURE_INCOMPLETE
from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers.redtiger.feature_sessions import build_redtiger_feature_sessions
from tester_spin.providers.redtiger.farm_adapter import RedTigerProvider


def _result(root: Path, *, kind: str = "PURCHASE", mode_id: str = "PURCHASE_FREE") -> GameTestResult:
    attempt_dir = root / mode_id / "attempt-00001"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    attempt = SpinAttempt(
        number=1,
        ok=True,
        mode_id=mode_id,
        mode_kind=kind,
        terminal=True,
        wire_steps=1,
        artifact_dir=str(attempt_dir),
    )
    return GameTestResult(
        provider="redtiger",
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


def _feature_response(rounds: int) -> dict:
    return {
        "success": True,
        "result": {
            "hasState": True,
            "features": [
                {"spinMode": "FREE", "hasState": index < rounds - 1, "win": index + 1}
                for index in range(rounds)
            ],
        },
    }


class RedTigerFeatureSessionTests(unittest.TestCase):
    def test_response_tree_leaf_outcomes_are_logical_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root)
            attempt = Path(result.attempts[0].artifact_dir)
            (attempt / "request.json").write_text(json.dumps({"extras": {"features": {"featureBuy": "FREE"}}}), encoding="utf-8")
            (attempt / "response.json").write_text(json.dumps(_feature_response(10)), encoding="utf-8")

            report = build_redtiger_feature_sessions(result)

        session = report["sessions"][0]
        self.assertEqual(session["trigger"], "PURCHASE")
        self.assertEqual(session["totals"]["logical_rounds"], 10)
        self.assertEqual(session["totals"]["wire_steps"], 1)
        self.assertEqual(session["state"], FEATURE_COMPLETE)

    def test_uncovered_choice_sibling_keeps_session_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root)
            attempt = Path(result.attempts[0].artifact_dir)
            (attempt / "request.json").write_text(json.dumps({"extras": {"features": {"featureBuy": "FREE"}}}), encoding="utf-8")
            (attempt / "response.json").write_text(json.dumps(_feature_response(2)), encoding="utf-8")
            result.discovered_modes = [
                {
                    "id": "PURCHASE_FREE__BRANCH_ROOT",
                    "kind": "CHOICE_BRANCH",
                    "parent": "PURCHASE_FREE",
                    "prefix": [],
                    "wire_command": "platform/game/choice",
                    "required_options": ["LEFT", "RIGHT"],
                    "covered_options": ["LEFT"],
                    "required_samples": 1,
                    "sample_counts": {"LEFT": 1, "RIGHT": 0},
                }
            ]

            report = build_redtiger_feature_sessions(result)

        self.assertEqual(report["sessions"][0]["state"], FEATURE_INCOMPLETE)
        self.assertEqual(report["sessions"][0]["choices"][0]["missing_options"], ["RIGHT"])

    def test_closed_choice_graph_preserves_complete_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root)
            attempt = Path(result.attempts[0].artifact_dir)
            (attempt / "request.json").write_text(json.dumps({"extras": {"features": {"featureBuy": "FREE"}}}), encoding="utf-8")
            (attempt / "response.json").write_text(json.dumps(_feature_response(2)), encoding="utf-8")
            result.discovered_modes = [
                {
                    "id": "PURCHASE_FREE__BRANCH_ROOT",
                    "kind": "CHOICE_BRANCH",
                    "parent": "PURCHASE_FREE",
                    "prefix": [],
                    "wire_command": "platform/game/choice",
                    "required_options": ["LEFT", "RIGHT"],
                    "covered_options": ["LEFT", "RIGHT"],
                    "required_samples": 1,
                    "sample_counts": {"LEFT": 1, "RIGHT": 1},
                }
            ]
            report = build_redtiger_feature_sessions(result)
        self.assertEqual(report["sessions"][0]["state"], FEATURE_COMPLETE)

    def test_active_provider_uses_redtiger_normalizer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, kind="SPIN", mode_id="SPIN")
            attempt = Path(result.attempts[0].artifact_dir)
            (attempt / "request.json").write_text(json.dumps({"stake": "1"}), encoding="utf-8")
            (attempt / "response.json").write_text(json.dumps(_feature_response(2)), encoding="utf-8")
            provider = RedTigerProvider(root / "data")
            report = provider.build_feature_sessions(result)
        self.assertEqual(report["authority"], "redtiger-result-tree+choice-branch-coverage")
        self.assertEqual(report["sessions"][0]["trigger"], "NATURAL")


if __name__ == "__main__":
    unittest.main()
