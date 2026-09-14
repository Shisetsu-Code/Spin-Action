from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.rubyplay.execution import RubyPlayExecutionMixin
from tester_spin.providers.rubyplay.runtime import (
    LauncherConfig,
    RubyPlayClientProfile,
    RubyPlayRuntime,
)


def _result(*, modes: list[dict], status: str = "OK") -> GameTestResult:
    return GameTestResult(
        provider="rubyplay",
        slug="demo",
        game_name="Demo",
        game_url="https://rubyplay.com/games/demo/",
        requested_spins=1,
        successful_spins=1,
        failed_spins=0,
        status=status,
        discovered_modes=modes,
    )


def _annotate(result: GameTestResult, *, capability, available, feature_type="") -> GameTestResult:
    from tester_spin.providers.rubyplay.action_inventory import (
        annotate_rubyplay_action_inventory,
    )

    runtime = SimpleNamespace(
        client_profile=SimpleNamespace(
            buy_feature_client_supported=capability,
            buy_feature_type=feature_type,
            buy_feature_multiplier=50.0 if feature_type else None,
            wager=2.0,
        ),
        init_data={"data": {"buy_feature_available": available}},
    )
    return annotate_rubyplay_action_inventory(result, runtime)


class _Session:
    def close(self) -> None:
        return None


class _Response:
    status_code = 200


class _Provider(RubyPlayExecutionMixin):
    key = "rubyplay"

    def __init__(self, root: Path) -> None:
        self.root = root

    def game_dir(self, game: Game) -> Path:
        path = self.root / game.slug
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _new_session(self) -> _Session:
        return _Session()


class RubyPlayActionInventoryTests(unittest.TestCase):
    def test_client_proven_absence_closes_spin_only_root_inventory(self) -> None:
        result = _result(
            modes=[
                {"id": "SPIN", "kind": "SPIN", "observed": True, "executable": True}
            ]
        )

        _annotate(result, capability=False, available=False)

        inventory = result.structural_map["action_inventory"]
        self.assertEqual(inventory["state"], "COMPLETE")
        self.assertEqual(inventory["root_actions"], ["SPIN"])
        self.assertEqual(inventory["missing_root_actions"], [])
        self.assertEqual(inventory["unexpected_root_actions"], [])

    def test_enabled_buy_feature_requires_matching_purchase_root(self) -> None:
        result = _result(
            modes=[
                {"id": "SPIN", "kind": "SPIN", "observed": True, "executable": True},
                {
                    "id": "PURCHASE_FREESPIN",
                    "kind": "PURCHASE",
                    "observed": True,
                    "executable": True,
                    "wire_command": "buy_feature",
                    "buy_feature_type": "freespin",
                },
            ]
        )

        _annotate(result, capability=True, available=True, feature_type="freespin")

        inventory = result.structural_map["action_inventory"]
        self.assertEqual(inventory["state"], "COMPLETE")
        self.assertEqual(
            inventory["root_actions"],
            ["PURCHASE_FREESPIN", "SPIN"],
        )

    def test_unresolved_client_capability_never_closes_inventory(self) -> None:
        result = _result(
            modes=[
                {"id": "SPIN", "kind": "SPIN", "observed": True, "executable": True}
            ]
        )

        _annotate(result, capability=None, available=False)

        inventory = result.structural_map["action_inventory"]
        self.assertEqual(inventory["state"], "UNKNOWN")
        self.assertIn("unresolved", inventory["reason"].lower())

    def test_execution_attaches_inventory_from_its_bootstrap_runtime(self) -> None:
        profile = RubyPlayClientProfile(
            protocol_version=2,
            math_version=20220204,
            wager=2.0,
            buy_feature_client_supported=False,
            actions=["init", "spin"],
        )
        runtime = RubyPlayRuntime(
            session=_Session(),  # type: ignore[arg-type]
            launcher=LauncherConfig(
                launcher_url="https://launcher.example/launcher?x=1",
                gamename="rp_72",
                operator="rubyplay.com",
                server_url="https://srv.example",
                currency="EUR",
                mode="fun",
                lang="en",
            ),
            client_profile=profile,
            session_key="opaque",
            fun_mode_data={"gameId": 72},
            init_data={"data": {"buy_feature_available": False}},
            bets=[2, 5, 10],
            default_bet=2,
            default_bet_index=0,
            currency="EUR",
            subunit=100,
            action_number=0,
            next_action="spin",
        )
        game = Game(
            provider="rubyplay",
            slug="diamond-explosion-7s",
            name="Diamond Explosion 7s",
            url="https://rubyplay.com/games/diamond-explosion-7s/",
        )

        def fake_post(runtime_obj, action, **kwargs):
            previous = runtime_obj.action_number
            runtime_obj.action_number += 1
            runtime_obj.next_action = "spin"
            return (
                _Response(),
                {"action": action, "bet": kwargs.get("bet")},
                {
                    "status": "ok",
                    "data": {
                        "an": runtime_obj.action_number,
                        "next_action": "spin",
                        "player": {"balance": 1000000},
                    },
                },
                previous,
            )

        with tempfile.TemporaryDirectory() as temp:
            provider = _Provider(Path(temp))
            with (
                patch("tester_spin.providers.rubyplay.execution.bootstrap_game", return_value=runtime),
                patch("tester_spin.providers.rubyplay.execution.post_action", side_effect=fake_post),
                patch("tester_spin.providers.rubyplay.execution.validate_action_response", return_value=[]),
            ):
                result = provider.test_game(
                    game,
                    spins=1,
                    timeout_s=5.0,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

        self.assertEqual(result.status, "OK")
        self.assertEqual(
            result.structural_map["action_inventory"]["state"],
            "COMPLETE",
        )
        self.assertEqual(
            result.structural_map["action_inventory"]["root_actions"],
            ["SPIN"],
        )


if __name__ == "__main__":
    unittest.main()
