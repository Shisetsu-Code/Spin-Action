from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.feature_sessions import FEATURE_COMPLETE, FEATURE_INCOMPLETE
from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers.rubyplay.feature_sessions import build_rubyplay_feature_sessions
from tester_spin.providers.rubyplay.farm_adapter import RubyPlayProvider


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _result(root: Path, *, mode_id: str, mode_kind: str, wire_steps: int, terminal: bool = True) -> GameTestResult:
    attempt_dir = root / mode_id / "attempt-00001"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    attempt = SpinAttempt(
        number=1,
        ok=terminal,
        mode_id=mode_id,
        mode_kind=mode_kind,
        status_code=200,
        terminal=terminal,
        wire_steps=wire_steps,
        artifact_dir=str(attempt_dir),
    )
    return GameTestResult(
        provider="rubyplay",
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


class RubyPlayFeatureSessionTests(unittest.TestCase):
    def test_purchase_counts_logical_feature_rounds_independently_from_wire_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, mode_id="PURCHASE_FREESPIN", mode_kind="PURCHASE", wire_steps=4)
            attempt = Path(result.attempts[0].artifact_dir)
            _write(attempt / "request.json", {"action": "buy_feature", "buy_feature_type": "freespin"})
            _write(attempt / "response.json", {"data": {"next_action": "freespin"}})
            for step, next_action in ((2, "freespin"), (3, "freespin"), (4, "spin")):
                _write(attempt / f"step-{step:03d}-request.json", {"action": "freespin"})
                _write(attempt / f"step-{step:03d}-response.json", {"data": {"next_action": next_action}})

            report = build_rubyplay_feature_sessions(result)

        self.assertEqual(report["session_count"], 1)
        session = report["sessions"][0]
        self.assertEqual(session["trigger"], "PURCHASE")
        self.assertEqual(session["parent_mode"], "PURCHASE_FREESPIN")
        self.assertEqual(session["totals"]["logical_rounds"], 3)
        self.assertEqual(session["totals"]["wire_steps"], 4)
        self.assertEqual(session["state"], FEATURE_COMPLETE)

    def test_natural_spin_feature_uses_same_session_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, mode_id="SPIN", mode_kind="SPIN", wire_steps=3)
            attempt = Path(result.attempts[0].artifact_dir)
            _write(attempt / "request.json", {"action": "spin"})
            _write(attempt / "response.json", {"data": {"next_action": "respin"}})
            _write(attempt / "step-002-request.json", {"action": "respin"})
            _write(attempt / "step-002-response.json", {"data": {"next_action": "respin"}})
            _write(attempt / "step-003-request.json", {"action": "respin"})
            _write(attempt / "step-003-response.json", {"data": {"next_action": "spin"}})

            report = build_rubyplay_feature_sessions(result)

        session = report["sessions"][0]
        self.assertEqual(session["trigger"], "NATURAL")
        self.assertEqual(session["totals"]["logical_rounds"], 2)
        self.assertEqual(session["state"], FEATURE_COMPLETE)

    def test_unknown_entry_next_action_is_preserved_as_incomplete_feature(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(
                root,
                mode_id="PURCHASE_MYSTERY",
                mode_kind="PURCHASE",
                wire_steps=1,
                terminal=False,
            )
            attempt = Path(result.attempts[0].artifact_dir)
            _write(
                attempt / "request.json",
                {"action": "buy_feature", "buy_feature_type": "mystery"},
            )
            _write(attempt / "response.json", {"data": {"next_action": "mystery_step"}})
            result.attempts[0].warning = "estado de continuación no automatizado: mystery_step"

            report = build_rubyplay_feature_sessions(result)

        self.assertEqual(report["session_count"], 1)
        session = report["sessions"][0]
        self.assertEqual(session["state"], FEATURE_INCOMPLETE)
        self.assertEqual(session["entry"]["next_action"], "mystery_step")
        self.assertIn("mystery_step", " ".join(session["reasons"]))

    def test_picker_domain_from_another_parent_cannot_close_this_purchase(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, mode_id="PURCHASE_SELECT", mode_kind="PURCHASE", wire_steps=2)
            attempt = Path(result.attempts[0].artifact_dir)
            _write(attempt / "request.json", {"action": "buy_feature", "buy_feature_type": "select"})
            _write(attempt / "response.json", {"data": {"next_action": "select"}})
            _write(attempt / "step-002-request.json", {"action": "select", "index": 0})
            _write(attempt / "step-002-response.json", {"data": {"next_action": "spin"}})
            result.discovered_modes = [
                {
                    "id": "OTHER__SELECT_INDEX_DOMAIN",
                    "kind": "INDEXED_CHOICE",
                    "parent": "PURCHASE_OTHER",
                    "wire_command": "select",
                    "coverage_required": True,
                    "required_options": ["0", "1"],
                    "covered_options": ["0", "1"],
                }
            ]

            report = build_rubyplay_feature_sessions(result)

        session = report["sessions"][0]
        self.assertEqual(session["state"], FEATURE_INCOMPLETE)
        self.assertEqual(session["choices"][0]["domain_state"], "UNRESOLVED")

    def test_parent_scoped_picker_domain_can_close_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, mode_id="PURCHASE_SELECT", mode_kind="PURCHASE", wire_steps=2)
            attempt = Path(result.attempts[0].artifact_dir)
            _write(attempt / "request.json", {"action": "buy_feature", "buy_feature_type": "select"})
            _write(attempt / "response.json", {"data": {"next_action": "select"}})
            _write(attempt / "step-002-request.json", {"action": "select", "index": 0})
            _write(attempt / "step-002-response.json", {"data": {"next_action": "spin"}})
            result.discovered_modes = [
                {
                    "id": "PURCHASE_SELECT__SELECT_INDEX_DOMAIN",
                    "kind": "INDEXED_CHOICE",
                    "parent": "PURCHASE_SELECT",
                    "wire_command": "select",
                    "coverage_required": True,
                    "required_options": ["0", "1"],
                    "covered_options": ["0", "1"],
                    "required_samples": 1,
                    "sample_counts": {"0": 1, "1": 1},
                }
            ]

            report = build_rubyplay_feature_sessions(result)

        session = report["sessions"][0]
        self.assertEqual(session["state"], FEATURE_COMPLETE)
        self.assertTrue(session["choices"][0]["complete"])

    def test_active_provider_build_hook_uses_rubyplay_normalizer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = _result(root, mode_id="SPIN", mode_kind="SPIN", wire_steps=2)
            attempt = Path(result.attempts[0].artifact_dir)
            _write(attempt / "request.json", {"action": "spin"})
            _write(attempt / "response.json", {"data": {"next_action": "respin"}})
            _write(attempt / "step-002-request.json", {"action": "respin"})
            _write(attempt / "step-002-response.json", {"data": {"next_action": "spin"}})
            provider = RubyPlayProvider(root / "data")

            report = provider.build_feature_sessions(result)

        self.assertEqual(report["authority"], "rubyplay-next_action+runtime-wire+parent-scoped-choice-domain")
        self.assertEqual(report["session_count"], 1)
        self.assertEqual(report["sessions"][0]["totals"]["logical_rounds"], 1)


if __name__ == "__main__":
    unittest.main()
