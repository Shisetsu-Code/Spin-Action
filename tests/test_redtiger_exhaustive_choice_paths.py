from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.redtiger.branch_coverage import ReplayOutcome, expand_all_choice_branches
from tester_spin.providers.redtiger.runtime import ChoicePrompt, response_summary


def terminal_payload(mode: str) -> dict:
    return {
        "success": True,
        "result": {
            "transactions": {"roundId": 99},
            "game": {
                "spinMode": mode,
                "stake": "2.00",
                "win": {"total": "0.00"},
                "features": [],
                "gameMode": 0,
                "hasState": False,
            },
        },
    }


def pending_payload(options: tuple[str, ...], mode: str = "SuperFreeSpins") -> dict:
    return {
        "success": True,
        "result": {
            "transactions": {"roundId": 42},
            "game": {
                "spinMode": mode,
                "stake": "2.00",
                "win": {"total": "0.00"},
                "choices": {"selected": None, "available": list(options)},
                "gameMode": 0,
                "hasState": True,
            },
        },
    }


class RedTigerExhaustiveChoicePathTests(unittest.TestCase):
    @staticmethod
    def _base_result(root: Path) -> GameTestResult:
        return GameTestResult(
            provider="redtiger",
            slug="synthetic",
            game_name="Synthetic",
            game_url="https://games.evolution.com/slots/synthetic/",
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
            symbol="RuntimeGame",
            run_dir=str(root),
            discovered_modes=[
                {
                    "id": "PURCHASE_SUPER",
                    "kind": "PURCHASE",
                    "feature_buy": "SuperFreeSpins",
                    "feature_multiplier": "300",
                },
                {
                    "id": "PURCHASE_SUPER__CHOICE_1",
                    "kind": "CHOICE_CONTINUATION",
                    "parent": "PURCHASE_SUPER",
                    "available": ["GOD_A", "GOD_B", "RANDOM"],
                    "selected_for_validation": "GOD_A",
                },
            ],
        )

    @staticmethod
    def _game() -> Game:
        return Game(
            provider="redtiger",
            slug="synthetic",
            name="Synthetic",
            url="https://games.evolution.com/slots/synthetic/",
            symbol="12345",
        )

    def test_every_root_choice_is_replayed_on_a_fresh_path(self) -> None:
        options = ("GOD_A", "GOD_B", "RANDOM")

        def fake_replay(_provider, _game, *, prefix, run_root, **kwargs):
            if not prefix:
                payload = pending_payload(options)
                prompt = ChoicePrompt(round_id=42, available=options)
            else:
                payload = terminal_payload(prefix[-1])
                prompt = None
            return ReplayOutcome(
                prompt=prompt,
                final_payload=payload,
                summaries=[response_summary(payload)],
                warnings=[],
                wire_steps=1 + len(prefix),
                status_code=200,
                selected=prefix,
                elapsed_ms=1.0,
                artifact_dir=run_root / "fake" / ("root" if not prefix else "-".join(prefix)),
            )

        with tempfile.TemporaryDirectory() as temp, patch(
            "tester_spin.providers.redtiger.branch_coverage._replay_prefix",
            side_effect=fake_replay,
        ) as replay:
            result = self._base_result(Path(temp))
            expand_all_choice_branches(
                SimpleNamespace(bootstrap_endpoints=None),
                self._game(),
                result,
                launch_id="12345",
                repetitions=1,
                timeout_s=5.0,
                stop_event=threading.Event(),
                progress=lambda _message: None,
            )

        prefixes = [call.kwargs["prefix"] for call in replay.call_args_list]
        self.assertEqual(set(prefixes), {(), ("GOD_A",), ("GOD_B",), ("RANDOM",)})
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.requested_spins, 4)
        self.assertEqual(result.successful_spins, 4)
        branch = next(item for item in result.discovered_modes if item.get("kind") == "CHOICE_BRANCH")
        self.assertEqual(branch["required_options"], list(options))
        self.assertEqual(branch["covered_options"], sorted(options))

    def test_nested_choices_expand_cartesian_paths(self) -> None:
        def fake_replay(_provider, _game, *, prefix, run_root, **kwargs):
            if prefix == ():
                options = ("A", "B")
                payload = pending_payload(options)
                prompt = ChoicePrompt(round_id=1, available=options)
            elif prefix == ("A",):
                options = ("X", "Y")
                payload = pending_payload(options, mode="Nested")
                prompt = ChoicePrompt(round_id=2, available=options)
            else:
                payload = terminal_payload(prefix[-1])
                prompt = None
            return ReplayOutcome(
                prompt=prompt,
                final_payload=payload,
                summaries=[response_summary(payload)],
                warnings=[],
                wire_steps=1 + len(prefix),
                status_code=200,
                selected=prefix,
                elapsed_ms=1.0,
                artifact_dir=run_root / "fake" / ("root" if not prefix else "-".join(prefix)),
            )

        with tempfile.TemporaryDirectory() as temp, patch(
            "tester_spin.providers.redtiger.branch_coverage._replay_prefix",
            side_effect=fake_replay,
        ) as replay:
            result = self._base_result(Path(temp))
            expand_all_choice_branches(
                SimpleNamespace(bootstrap_endpoints=None),
                self._game(),
                result,
                launch_id="12345",
                repetitions=1,
                timeout_s=5.0,
                stop_event=threading.Event(),
                progress=lambda _message: None,
            )

        prefixes = {call.kwargs["prefix"] for call in replay.call_args_list}
        self.assertEqual(prefixes, {(), ("A",), ("B",), ("A", "X"), ("A", "Y")})
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.requested_spins, 4)  # base + terminal leaves B, A/X and A/Y
        branch_points = [item for item in result.discovered_modes if item.get("kind") == "CHOICE_BRANCH"]
        self.assertEqual(len(branch_points), 2)
        self.assertTrue(all(set(item["required_options"]) == set(item["covered_options"]) for item in branch_points))

    def test_failed_choice_path_cannot_report_ok(self) -> None:
        options = ("LEFT", "RIGHT")

        def fake_replay(_provider, _game, *, prefix, run_root, **kwargs):
            if prefix == ():
                payload = pending_payload(options)
                return ReplayOutcome(
                    prompt=ChoicePrompt(round_id=1, available=options),
                    final_payload=payload,
                    summaries=[response_summary(payload)],
                    warnings=[],
                    wire_steps=1,
                    status_code=200,
                    selected=(),
                    elapsed_ms=1.0,
                    artifact_dir=run_root / "fake" / "root",
                )
            if prefix == ("RIGHT",):
                raise RuntimeError("wire rejected")
            payload = terminal_payload("LEFT")
            return ReplayOutcome(
                prompt=None,
                final_payload=payload,
                summaries=[response_summary(payload)],
                warnings=[],
                wire_steps=2,
                status_code=200,
                selected=prefix,
                elapsed_ms=1.0,
                artifact_dir=run_root / "fake" / "left",
            )

        with tempfile.TemporaryDirectory() as temp, patch(
            "tester_spin.providers.redtiger.branch_coverage._replay_prefix",
            side_effect=fake_replay,
        ):
            result = self._base_result(Path(temp))
            expand_all_choice_branches(
                SimpleNamespace(bootstrap_endpoints=None),
                self._game(),
                result,
                launch_id="12345",
                repetitions=1,
                timeout_s=5.0,
                stop_event=threading.Event(),
                progress=lambda _message: None,
            )

        self.assertEqual(result.status, "PARCIAL")
        self.assertGreater(result.failed_spins, 0)
        self.assertIn("RIGHT", result.error)


if __name__ == "__main__":
    unittest.main()
