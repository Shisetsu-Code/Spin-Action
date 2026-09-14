from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.action_audit import build_action_audit
from tester_spin.models import GameTestResult, SpinAttempt


def _result(*, status: str = "OK") -> GameTestResult:
    return GameTestResult(
        provider="pragmatic",
        slug="demo",
        game_name="Demo",
        game_url="https://example.invalid/game",
        requested_spins=1,
        successful_spins=1,
        failed_spins=0,
        status=status,
    )


def _terminal_attempt(mode_id: str = "SPIN") -> SpinAttempt:
    return SpinAttempt(
        number=1,
        ok=True,
        mode_id=mode_id,
        mode_kind="SPIN",
        terminal=True,
        wire_steps=1,
    )


class ActionAuditTests(unittest.TestCase):
    def test_complete_requires_closed_inventory_and_terminal_action_evidence(self) -> None:
        result = _result()
        result.structural_map = {
            "action_inventory": {
                "state": "COMPLETE",
                "source": "provider-authoritative-init",
                "reason": "provider enumerated the full action domain",
            }
        }
        result.discovered_modes = [
            {"id": "SPIN", "kind": "SPIN", "observed": True, "executable": True}
        ]
        result.attempts = [_terminal_attempt()]

        audit = build_action_audit(result)

        self.assertEqual(audit["verdict"], "COMPLETE")
        self.assertEqual(audit["actions"][0]["state"], "DEMONSTRATED")
        self.assertEqual(audit["unknown_reasons"], [])
        self.assertEqual(audit["missing_reasons"], [])

    def test_action_without_terminal_evidence_is_incomplete(self) -> None:
        result = _result()
        result.structural_map = {
            "action_inventory": {
                "state": "COMPLETE",
                "source": "provider-authoritative-init",
                "reason": "closed",
            }
        }
        result.discovered_modes = [
            {"id": "PURCHASE_1", "kind": "PURCHASE", "observed": True, "executable": True}
        ]
        result.attempts = []

        audit = build_action_audit(result)

        self.assertEqual(audit["verdict"], "INCOMPLETE")
        self.assertTrue(any("PURCHASE_1" in reason for reason in audit["missing_reasons"]))

    def test_missing_inventory_closure_is_unknown_not_partial(self) -> None:
        result = _result()
        result.discovered_modes = [
            {"id": "SPIN", "kind": "SPIN", "observed": True, "executable": True}
        ]
        result.attempts = [_terminal_attempt()]

        audit = build_action_audit(result)

        self.assertEqual(audit["verdict"], "UNKNOWN")
        self.assertTrue(any("inventario" in reason.lower() for reason in audit["unknown_reasons"]))

    def test_missing_branch_option_is_incomplete(self) -> None:
        result = _result()
        result.structural_map = {
            "action_inventory": {
                "state": "COMPLETE",
                "source": "provider-authoritative-init",
                "reason": "closed",
            }
        }
        result.discovered_modes = [
            {
                "id": "PURCHASE_1__CHOICE_ROOT",
                "kind": "CHOICE_BRANCH",
                "coverage_required": True,
                "required_options": ["0", "1"],
                "covered_options": ["0"],
                "observed": True,
                "executable": True,
            }
        ]

        audit = build_action_audit(result)

        self.assertEqual(audit["verdict"], "INCOMPLETE")
        self.assertEqual(audit["actions"][0]["missing_options"], ["1"])

    def test_terminal_parent_attempt_credits_observed_continuation_wire_commands(self) -> None:
        result = _result()
        result.provider = "rubyplay"
        result.structural_map = {
            "action_inventory": {
                "state": "COMPLETE",
                "source": "provider-authoritative-init",
                "reason": "closed",
            }
        }
        result.discovered_modes = [
            {
                "id": "SPIN",
                "kind": "SPIN",
                "observed": True,
                "executable": True,
                "wire_command": "spin",
            },
            {
                "id": "CONTINUATION_FREESPIN",
                "kind": "CONTINUATION",
                "observed": True,
                "executable": True,
                "wire_command": "freespin",
            },
            {
                "id": "CONTINUATION_RESPIN",
                "kind": "CONTINUATION",
                "observed": True,
                "executable": True,
                "wire_command": "respin",
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt_dir = root / "SPIN" / "attempt-00001"
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "request.json").write_text(
                json.dumps({"action": "spin"}), encoding="utf-8"
            )
            (attempt_dir / "step-002-request.json").write_text(
                json.dumps({"action": "freespin"}), encoding="utf-8"
            )
            (attempt_dir / "step-003-request.json").write_text(
                json.dumps({"action": "respin"}), encoding="utf-8"
            )
            result.run_dir = str(root)
            attempt = _terminal_attempt()
            attempt.wire_steps = 3
            attempt.artifact_dir = str(attempt_dir)
            result.attempts = [attempt]

            audit = build_action_audit(result)

        self.assertEqual(audit["verdict"], "COMPLETE")
        states = {item["id"]: item for item in audit["actions"]}
        self.assertEqual(states["CONTINUATION_FREESPIN"]["state"], "DEMONSTRATED")
        self.assertEqual(states["CONTINUATION_RESPIN"]["state"], "DEMONSTRATED")
        self.assertIn("remote wire executions=1", states["CONTINUATION_FREESPIN"]["evidence"])
        self.assertIn("remote wire executions=1", states["CONTINUATION_RESPIN"]["evidence"])

    def test_path_coverage_artifact_can_block_complete(self) -> None:
        result = _result()
        result.structural_map = {
            "action_inventory": {
                "state": "COMPLETE",
                "source": "provider-authoritative-init",
                "reason": "closed",
            }
        }
        result.discovered_modes = [
            {"id": "SPIN", "kind": "SPIN", "observed": True, "executable": True}
        ]
        result.attempts = [_terminal_attempt()]
        with tempfile.TemporaryDirectory() as tmp:
            result.run_dir = tmp
            Path(tmp, "path-coverage.json").write_text(
                json.dumps(
                    {
                        "complete": False,
                        "branch_points": [
                            {
                                "mode_id": "SPIN",
                                "signature": "SPIN:choice",
                                "missing": ["2"],
                                "complete": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            audit = build_action_audit(result)

        self.assertEqual(audit["verdict"], "INCOMPLETE")
        self.assertTrue(any("path-coverage" in reason for reason in audit["missing_reasons"]))

    def test_error_and_cancelled_are_never_complete(self) -> None:
        error_audit = build_action_audit(_result(status="ERROR"))
        cancelled_audit = build_action_audit(_result(status="CANCELADO"))

        self.assertEqual(error_audit["verdict"], "ERROR")
        self.assertEqual(cancelled_audit["verdict"], "CANCELLED")


if __name__ == "__main__":
    unittest.main()
