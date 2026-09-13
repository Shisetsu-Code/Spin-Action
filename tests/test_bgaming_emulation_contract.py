from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import Game, GameTestResult, SpinAttempt
from tester_spin.providers.bgaming.adapter import BGamingProvider
from tester_spin.providers.bgaming.emulation_contract import generate_bgaming_emulation_contract


class BGamingEmulationContractTests(unittest.TestCase):
    @staticmethod
    def _write(path: Path, payload) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _game() -> Game:
        return Game(
            provider="bgaming",
            slug="synthetic",
            name="Synthetic BGaming",
            url="https://example.invalid/game",
            symbol="SyntheticGame",
        )

    @staticmethod
    def _result(root: Path) -> GameTestResult:
        return GameTestResult(
            provider="bgaming",
            slug="synthetic",
            game_name="Synthetic BGaming",
            game_url="https://example.invalid/game",
            requested_spins=2,
            successful_spins=2,
            failed_spins=0,
            status="OK",
            symbol="SyntheticGame",
            run_dir=str(root),
            discovered_modes=[
                {
                    "id": "SPIN",
                    "kind": "SPIN",
                    "observed": True,
                    "executable": True,
                    "wire_command": "spin",
                },
                {
                    "id": "PURCHASE_BONUS_BUY",
                    "kind": "PURCHASE",
                    "observed": True,
                    "executable": True,
                    "wire_command": "spin",
                    "purchased_feature": "bonus_buy",
                    "cost_multiplier": 100,
                },
            ],
            attempts=[
                SpinAttempt(number=1, ok=True, mode_id="SPIN", terminal=True, artifact_dir=str(root / "SPIN" / "attempt-001")),
                SpinAttempt(number=1, ok=True, mode_id="PURCHASE_BONUS_BUY", terminal=True, artifact_dir=str(root / "PURCHASE_BONUS_BUY" / "attempt-001")),
            ],
        )

    def _api_v2_artifacts(self, root: Path) -> None:
        self._write(root / "profile.json", {"family": "api-v2"})
        self._write(
            root / "init-response.json",
            {
                "api_version": "2",
                "flow": {
                    "state": "ready",
                    "command": "init",
                    "available_actions": ["init", "spin"],
                },
                "balance": {"wallet": 100000, "game": 0},
            },
        )
        spin = root / "SPIN" / "attempt-001"
        self._write(
            spin / "step-001-request.json",
            {
                "command": "spin",
                "options": {"bet": 90, "volatility": "low"},
                "extra_data": {"round_series_id": 111},
            },
        )
        self._write(
            spin / "step-001-response.json",
            {
                "outcome": {"bet": 90, "win": 0, "screen": [[1, 2, 3]]},
                "balance": {"wallet": 99910, "game": 0},
                "flow": {
                    "round_id": "r1",
                    "last_action_id": "r1_1",
                    "state": "closed",
                    "command": "spin",
                    "available_actions": ["init", "spin"],
                },
            },
        )

        purchase = root / "PURCHASE_BONUS_BUY" / "attempt-001"
        self._write(
            purchase / "step-001-request.json",
            {
                "command": "spin",
                "options": {
                    "bet": 90,
                    "volatility": "low",
                    "purchased_feature": "bonus_buy",
                },
                "extra_data": {"round_series_id": 222},
            },
        )
        self._write(
            purchase / "step-001-response.json",
            {
                "outcome": {"bet": 90, "win": 0, "screen": None},
                "flow": {
                    "round_id": "r2",
                    "last_action_id": "r2_1",
                    "state": "preselection_game",
                    "command": "spin",
                    "available_actions": ["init", "preselection_game"],
                    "purchased_feature": {"name": "bonus_buy"},
                },
            },
        )
        self._write(
            purchase / "step-002-request.json",
            {
                "command": "preselection_game",
                "extra_data": {"round_series_id": 222},
            },
        )
        self._write(
            purchase / "step-002-response.json",
            {
                "outcome": {"bet": 0, "win": 9000, "screen": None},
                "features": {"bonus_data": {"multiplier": 100}},
                "flow": {
                    "round_id": "r2",
                    "last_action_id": "r2_2",
                    "state": "closed",
                    "command": "preselection_game",
                    "available_actions": ["init", "spin"],
                },
            },
        )

    def test_api_v2_generates_backend_dispatch_and_state_machine(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "run"
            root.mkdir()
            self._api_v2_artifacts(root)
            contract = generate_bgaming_emulation_contract(self._game(), self._result(root))

            self.assertTrue(contract["wire_replay_complete"])
            self.assertFalse(contract["rng_probabilities_known"])
            self.assertFalse(contract["math_model_complete"])
            by_id = {item["id"]: item for item in contract["request_contracts"]}
            self.assertIn("SPIN", by_id)
            self.assertEqual(by_id["SPIN"]["selector_domains_observed"]["volatility"], ["low"])
            self.assertIn("SPIN__PURCHASE_BONUS_BUY", by_id)

            machine = json.loads((root / "state-machine.json").read_text(encoding="utf-8"))
            transitions = machine["transitions"]
            self.assertTrue(
                any(
                    item["source"] == "READY"
                    and item["request_contract"] == "SPIN__PURCHASE_BONUS_BUY"
                    and item["target"] == "FLOW:preselection_game"
                    for item in transitions
                )
            )
            self.assertTrue(
                any(
                    item["source"] == "FLOW:preselection_game"
                    and item["request_contract"] == "PRESELECTION_GAME"
                    and item["target"] == "READY"
                    for item in transitions
                )
            )

            plan = json.loads((root / "backend-response-plan.json").read_text(encoding="utf-8"))
            self.assertIn("READY", plan["states"])
            self.assertIn("SPIN", plan["states"]["READY"])
            self.assertTrue((root / "backend-conformance.json").is_file())
            self.assertTrue(any((root / "response-schemas").iterdir()))

    def test_hyperhive_round_active_is_a_distinct_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "run"
            root.mkdir()
            self._write(root / "profile.json", {"family": "hyperhive-jsonrpc"})
            self._write(root / "init-response.json", {"result": {"config": {}, "balance": 1000}})
            attempt = root / "SPIN" / "attempt-001"
            self._write(
                attempt / "step-001-request.json",
                {
                    "id": "rpc-1",
                    "jsonrpc": "2.0",
                    "method": "play",
                    "params": {"token": "<redacted>", "req": {"bet": 100, "bet_type": "bet"}},
                },
            )
            self._write(
                attempt / "step-001-response.json",
                {"result": {"final": False, "nextAction": "spin", "balance": 900, "resp": {}}},
            )
            self._write(
                attempt / "step-002-request.json",
                {
                    "id": "rpc-2",
                    "jsonrpc": "2.0",
                    "method": "play",
                    "params": {"token": "<redacted>", "req": {"bet": 100, "bet_type": "bet"}},
                },
            )
            self._write(
                attempt / "step-002-response.json",
                {"result": {"final": True, "balance": 900, "resp": {"totalWin": 0}}},
            )
            result = self._result(root)
            result.requested_spins = 1
            result.successful_spins = 1
            result.attempts = [SpinAttempt(number=1, ok=True, mode_id="SPIN", terminal=True)]
            result.discovered_modes = [
                {"id": "SPIN", "kind": "SPIN", "observed": True, "executable": True}
            ]

            generate_bgaming_emulation_contract(self._game(), result)
            machine = json.loads((root / "state-machine.json").read_text(encoding="utf-8"))
            self.assertTrue(
                any(item["source"] == "READY" and item["target"] == "ROUND_ACTIVE" for item in machine["transitions"])
            )
            self.assertTrue(
                any(item["source"] == "ROUND_ACTIVE" and item["target"] == "READY" for item in machine["transitions"])
            )

    def test_provider_finalizer_emits_contract_after_global_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp) / "data"
            run_root = Path(temp) / "run"
            run_root.mkdir()
            self._api_v2_artifacts(run_root)
            result = self._result(run_root)
            provider = BGamingProvider(data_root)
            messages: list[str] = []

            finalized = provider.finalize_test_result(result, progress=messages.append)

            self.assertEqual(finalized.status, "OK")
            self.assertTrue((run_root / "emulation-contract.json").is_file())
            self.assertTrue(any("emulation contract" in item for item in messages))


if __name__ == "__main__":
    unittest.main()
