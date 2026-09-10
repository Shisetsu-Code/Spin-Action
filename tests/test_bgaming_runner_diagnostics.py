from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.providers.bgaming.runner_diagnostics import diagnose_progress_event


class BGamingRunnerDiagnosticsTests(unittest.TestCase):
    def test_api_continuation_failure_is_not_reported_as_initial_purchase_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp)
            attempt = (
                game_dir
                / "tests"
                / "2026-09-10_10-00-00-bgaming-http-api-v2"
                / "PURCHASE_FREESPIN_BUY"
                / "attempt-001"
            )
            attempt.mkdir(parents=True)
            (attempt / "step-001-request.json").write_text(
                json.dumps(
                    {
                        "command": "spin",
                        "options": {
                            "bet": 25,
                            "purchased_feature": "freespin_buy",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (attempt / "step-001-response.json").write_text(
                json.dumps({"flow": {"state": "freespins"}}),
                encoding="utf-8",
            )
            (attempt / "http-freespin-400.json").write_text(
                json.dumps({"status": 400}),
                encoding="utf-8",
            )

            diagnostic = diagnose_progress_event(
                game_dir,
                "[Game] PURCHASE_FREESPIN_BUY 1/1: ERROR HTTPError: "
                "400 Client Error: Bad Request",
            )

            self.assertIsNotNone(diagnostic)
            self.assertTrue(diagnostic["initial_mode_accepted"])
            self.assertEqual(diagnostic["failed_command"], "freespin")
            self.assertIn("entrada inicial del modo fue aceptada", diagnostic["message"])
            self.assertIn("continuación 'freespin'", diagnostic["message"])

    def test_api_initial_spin_failure_is_reported_as_initial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp)
            attempt = (
                game_dir
                / "tests"
                / "2026-09-10_10-00-00-bgaming-http-api-v2"
                / "SPIN"
                / "attempt-001"
            )
            attempt.mkdir(parents=True)
            (attempt / "http-spin-422.json").write_text(
                json.dumps({"status": 422}),
                encoding="utf-8",
            )

            diagnostic = diagnose_progress_event(
                game_dir,
                "[Game] SPIN 1/1: ERROR HTTPError: "
                "422 Client Error: Unprocessable Entity",
            )

            self.assertIsNotNone(diagnostic)
            self.assertFalse(diagnostic["initial_mode_accepted"])
            self.assertEqual(diagnostic["failed_command"], "spin")
            self.assertIn("falló el request inicial", diagnostic["message"])

    def test_hyperhive_rejection_reports_actual_outbound_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp)
            attempt = (
                game_dir
                / "tests"
                / "2026-09-10_10-00-00-bgaming-hyperhive-jsonrpc"
                / "SPIN"
                / "attempt-001"
            )
            attempt.mkdir(parents=True)
            (attempt / "rpc-error-request.json").write_text(
                json.dumps(
                    {
                        "id": "uuid-value",
                        "jsonrpc": "2.0",
                        "method": "play",
                        "params": {
                            "token": "<redacted>",
                            "state_lock": "<redacted>",
                            "req": {
                                "bet": 100,
                                "bet_type": "bet",
                                "custom_req": {
                                    "isNormalBuy": False,
                                    "isSuperBuy": False,
                                    "action": "spin",
                                    "exponent": 2,
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            diagnostic = diagnose_progress_event(
                game_dir,
                "[Game] SPIN 1/1: ERROR HyperHiveRPCError: BGaming HyperHive RPC error",
            )

            self.assertIsNotNone(diagnostic)
            self.assertEqual(
                diagnostic["req_keys"],
                ["bet", "bet_type", "custom_req"],
            )
            self.assertEqual(
                diagnostic["custom_keys"],
                ["action", "exponent", "isNormalBuy", "isSuperBuy"],
            )
            self.assertEqual(diagnostic["custom_values"]["isNormalBuy"], False)
            self.assertEqual(diagnostic["custom_values"]["isSuperBuy"], False)
            self.assertTrue(diagnostic["state_lock_present"])

    def test_bootstrap_only_har_is_explicitly_not_protocol_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            diagnostic = diagnose_progress_event(
                Path(temp),
                "[Game] HAR usado por runner: analysis\\browser.har; "
                "calidad=HAR_BOOTSTRAP_ONLY, plays=0, spins=0, compras=0.",
            )
            self.assertIsNotNone(diagnostic)
            self.assertEqual(diagnostic["kind"], "har_not_protocol_usable")
            self.assertIn("no aporta templates", diagnostic["message"])


if __name__ == "__main__":
    unittest.main()
