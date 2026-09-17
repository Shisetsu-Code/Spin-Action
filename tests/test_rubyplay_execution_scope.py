from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tester_spin.models import Game
from tester_spin.providers.rubyplay import execution
from tester_spin.providers.rubyplay.runtime import (
    LauncherConfig,
    RubyPlayClientProfile,
    RubyPlayRuntime,
)


class _Session:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Response:
    status_code = 200


class _Provider(execution.RubyPlayExecutionMixin):
    key = "rubyplay"

    def __init__(self, root: Path, scope: str) -> None:
        self.root = root
        self.scope = scope

    def game_dir(self, game: Game) -> Path:
        path = self.root / game.slug
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _new_session(self):
        return _Session()

    def rubyplay_execution_scope(self) -> str:
        return self.scope


def _runtime(*, buy: bool) -> RubyPlayRuntime:
    profile = RubyPlayClientProfile(
        protocol_version=2,
        math_version=123,
        wager=10.0,
        buy_feature_type="select" if buy else "",
        buy_feature_multiplier=50.0 if buy else None,
        buy_feature_client_supported=buy,
        actions=["init", "spin"] + (["buy_feature", "select"] if buy else []),
    )
    return RubyPlayRuntime(
        session=_Session(),
        launcher=LauncherConfig(
            launcher_url="https://example.invalid/launcher?gamename=rp_test&operator=x&server_url=https://srv.example.invalid&currency=EUR&mode=fun&lang=en",
            gamename="rp_test",
            operator="x",
            server_url="https://srv.example.invalid",
            currency="EUR",
            mode="fun",
            lang="en",
        ),
        client_profile=profile,
        session_key="secret",
        fun_mode_data={"gameId": 1},
        init_data={"data": {"buy_feature_available": buy}},
        bets=[10],
        default_bet=10,
        default_bet_index=0,
        currency="EUR",
        subunit=100,
        action_number=1,
        next_action="spin",
    )


class RubyPlayExecutionScopeTests(unittest.TestCase):
    def test_scope_filter_contract(self) -> None:
        self.assertTrue(execution.scope_allows_mode("ALL", "SPIN"))
        self.assertTrue(execution.scope_allows_mode("ALL", "PURCHASE"))
        self.assertTrue(execution.scope_allows_mode("NATURAL_ONLY", "SPIN"))
        self.assertFalse(execution.scope_allows_mode("NATURAL_ONLY", "PURCHASE"))
        self.assertFalse(execution.scope_allows_mode("PURCHASE_ONLY", "SPIN"))
        self.assertTrue(execution.scope_allows_mode("PURCHASE_ONLY", "PURCHASE"))

    def _run(self, scope: str, *, buy: bool, spins: int = 1):
        calls: list[str] = []
        runtime = _runtime(buy=buy)

        def post_action(rt, action, **kwargs):
            calls.append(action)
            rt.next_action = "spin"
            body = {
                "status": "ok",
                "topic": f"gameserver/{action}",
                "data": {"an": rt.action_number + 1, "next_action": "spin"},
                "funModeData": dict(rt.fun_mode_data),
            }
            if action == "buy_feature":
                body["data"]["buy_feature_type"] = "select"
                body["data"]["buy_feature_price"] = 5000
            previous = rt.action_number
            rt.action_number += 1
            request = {"action": action, "an": previous, "key": "secret"}
            return _Response(), request, body, previous

        with tempfile.TemporaryDirectory() as temp:
            provider = _Provider(Path(temp), scope)
            game = Game(
                provider="rubyplay",
                slug="g",
                name="G",
                url="https://example.invalid/g",
            )
            with (
                patch.object(execution, "bootstrap_game", return_value=runtime),
                patch.object(execution, "post_action", side_effect=post_action),
                patch.object(execution, "validate_action_response", return_value=[]),
            ):
                result = provider.test_game(
                    game,
                    spins=spins,
                    timeout_s=5.0,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )
        return result, calls

    def test_natural_only_executes_spins_but_not_purchase(self) -> None:
        result, calls = self._run("NATURAL_ONLY", buy=True, spins=2)
        self.assertEqual(calls, ["spin", "spin"])
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.requested_spins, 2)

    def test_purchase_only_executes_purchase_but_not_base_spin(self) -> None:
        result, calls = self._run("PURCHASE_ONLY", buy=True, spins=1)
        self.assertEqual(calls, ["buy_feature"])
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.requested_spins, 1)

    def test_purchase_only_with_proven_absence_needs_no_spin_attempt(self) -> None:
        result, calls = self._run("PURCHASE_ONLY", buy=False, spins=1)
        self.assertEqual(calls, [])
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.requested_spins, 0)
        self.assertEqual(result.successful_spins, 0)
        self.assertEqual(result.failed_spins, 0)
        self.assertEqual(result.structural_map["action_inventory"]["state"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
