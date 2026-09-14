from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.action_audit import build_action_audit
from tester_spin.audit_wire_evidence import successful_wire_commands
from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.sample_catalog import build_sample_catalog


class RubyPlayNaturalEventRegressionTests(unittest.TestCase):
    def test_observed_15_step_chain_is_detected_and_audited_offline(self) -> None:
        # Exact action/next_action sequence observed in the live RubyPlay rp_72
        # attempt that naturally expanded to 15 wire steps. No semantics beyond
        # the provider's wire action names are inferred here.
        rare_chain = [
            ("spin", "freespin"),
            ("freespin", "respin"),
            ("respin", "respin"),
            ("respin", "respin"),
            ("respin", "respin"),
            ("respin", "respin"),
            ("respin", "respin"),
            ("respin", "freespin"),
            ("freespin", "freespin"),
            ("freespin", "freespin"),
            ("freespin", "freespin"),
            ("freespin", "freespin"),
            ("freespin", "freespin"),
            ("freespin", "freespin"),
            ("freespin", "spin"),
        ]

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempts: list[SpinAttempt] = []

            # Two ordinary terminal spins establish the dominant wire sequence.
            for number in (1, 2):
                directory = root / "SPIN" / f"attempt-{number:05d}"
                directory.mkdir(parents=True)
                (directory / "request.json").write_text(
                    json.dumps({"action": "spin"}), encoding="utf-8"
                )
                (directory / "response.json").write_text(
                    json.dumps({"status": "ok", "data": {"next_action": "spin"}}),
                    encoding="utf-8",
                )
                attempts.append(
                    SpinAttempt(
                        number=number,
                        ok=True,
                        terminal=True,
                        mode_id="SPIN",
                        mode_kind="SPIN",
                        wire_steps=1,
                        artifact_dir=str(directory),
                    )
                )

            directory = root / "SPIN" / "attempt-00003"
            directory.mkdir(parents=True)
            for step, (action, next_action) in enumerate(rare_chain, start=1):
                request_name = "request.json" if step == 1 else f"step-{step:03d}-request.json"
                response_name = "response.json" if step == 1 else f"step-{step:03d}-response.json"
                (directory / request_name).write_text(
                    json.dumps({"action": action}), encoding="utf-8"
                )
                (directory / response_name).write_text(
                    json.dumps(
                        {
                            "status": "ok",
                            "data": {"an": 33 + step, "next_action": next_action},
                        }
                    ),
                    encoding="utf-8",
                )
            attempts.append(
                SpinAttempt(
                    number=3,
                    ok=True,
                    terminal=True,
                    mode_id="SPIN",
                    mode_kind="SPIN",
                    wire_steps=15,
                    artifact_dir=str(directory),
                )
            )

            result = GameTestResult(
                provider="rubyplay",
                slug="diamond-explosion-7s",
                game_name="Diamond Explosion 7s",
                game_url="https://rubyplay.com/games/diamond-explosion-7s/",
                requested_spins=3,
                successful_spins=3,
                failed_spins=0,
                status="OK",
                run_dir=str(root),
                attempts=attempts,
                discovered_modes=[
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
                ],
                structural_map={
                    "action_inventory": {
                        "state": "COMPLETE",
                        "source": "rubyplay-active-client+init-session",
                        "reason": "spin-only root inventory closed by active client/init",
                        "root_actions": ["SPIN"],
                        "missing_root_actions": [],
                        "unexpected_root_actions": [],
                    }
                },
            )

            catalog = build_sample_catalog(result, samples_per_path=1)
            audit = build_action_audit(result)
            commands = successful_wire_commands(result)

        group = catalog["groups"][0]
        self.assertEqual(len(group["observed_state_sequences"]), 2)
        self.assertEqual(len(group["unclassified_wire_variants"]), 1)
        variant = group["unclassified_wire_variants"][0]
        self.assertEqual(variant["classification"], "UNCLASSIFIED_WIRE_VARIANT")
        self.assertEqual(variant["first_attempt"], 3)
        self.assertNotIn("semantic", variant)

        self.assertEqual(commands["spin"], 3)
        self.assertEqual(commands["respin"], 6)
        self.assertEqual(commands["freespin"], 8)

        self.assertEqual(audit["verdict"], "COMPLETE")
        action_states = {row["id"]: row for row in audit["actions"]}
        self.assertEqual(action_states["SPIN"]["state"], "DEMONSTRATED")
        self.assertEqual(action_states["CONTINUATION_FREESPIN"]["state"], "DEMONSTRATED")
        self.assertEqual(action_states["CONTINUATION_RESPIN"]["state"], "DEMONSTRATED")
        self.assertEqual(
            action_states["CONTINUATION_FREESPIN"]["evidence"],
            "remote wire executions=8",
        )
        self.assertEqual(
            action_states["CONTINUATION_RESPIN"]["evidence"],
            "remote wire executions=6",
        )


if __name__ == "__main__":
    unittest.main()
