from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.rubyplay.choice_probe import replay_index_probe


class _Session:
    def close(self) -> None:
        return None


class _Provider:
    def _new_session(self):
        return _Session()


class RubyPlaySelectPathReplayTests(unittest.TestCase):
    def test_second_select_is_targeted_after_forcing_first_select_prefix(self) -> None:
        runtime = SimpleNamespace(
            session=_Session(),
            default_bet=10,
            next_action="spin",
            client_profile=SimpleNamespace(wager=10.0, buy_feature_multiplier=50.0),
        )
        select_indices: list[int] = []
        sequence = iter(["select", "freespin", "select", "spin"])

        def bootstrap(session, url, **kwargs):
            runtime.session = session
            return runtime

        def post_action(rt, action, **kwargs):
            if action == "select":
                select_indices.append(int(kwargs["action_index"]))
            rt.next_action = next(sequence)
            return (
                SimpleNamespace(status_code=200),
                {"action": action, "index": kwargs.get("action_index")},
                {"status": "ok", "data": {"next_action": rt.next_action}},
                0,
            )

        result = GameTestResult(
            provider="rubyplay",
            slug="g",
            game_name="G",
            game_url="https://example.invalid/g",
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
            discovered_modes=[
                {
                    "id": "PURCHASE_SELECT",
                    "kind": "PURCHASE",
                    "buy_feature_type": "select",
                    "default_price": 5000,
                }
            ],
        )
        game = Game(provider="rubyplay", slug="g", name="G", url=result.game_url)

        with tempfile.TemporaryDirectory() as temp:
            outcome = replay_index_probe(
                _Provider(),
                game,
                result,
                parent_mode="PURCHASE_SELECT",
                action="select",
                prefix=("select=1",),
                index=2,
                timeout_s=5.0,
                stop_event=threading.Event(),
                artifact_dir=Path(temp),
                bootstrap_fn=bootstrap,
                post_action_fn=post_action,
                validate_fn=lambda *args, **kwargs: [],
            )

        self.assertEqual(outcome["outcome"], "TERMINAL")
        self.assertEqual(select_indices, [1, 2])
        self.assertEqual(outcome["prefix"], ["select=1"])
        self.assertTrue(outcome["target_reached"])

    def test_prefix_action_mismatch_is_not_silently_rewritten(self) -> None:
        runtime = SimpleNamespace(
            session=_Session(),
            default_bet=10,
            next_action="spin",
            client_profile=SimpleNamespace(wager=10.0, buy_feature_multiplier=50.0),
        )
        sequence = iter(["pick", "spin"])

        def bootstrap(session, url, **kwargs):
            runtime.session = session
            return runtime

        def post_action(rt, action, **kwargs):
            rt.next_action = next(sequence)
            return (
                SimpleNamespace(status_code=200),
                {"action": action},
                {"status": "ok", "data": {"next_action": rt.next_action}},
                0,
            )

        result = GameTestResult(
            provider="rubyplay",
            slug="g",
            game_name="G",
            game_url="https://example.invalid/g",
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
            discovered_modes=[{"id": "SPIN", "kind": "SPIN"}],
        )
        game = Game(provider="rubyplay", slug="g", name="G", url=result.game_url)

        with tempfile.TemporaryDirectory() as temp:
            outcome = replay_index_probe(
                _Provider(),
                game,
                result,
                parent_mode="SPIN",
                action="select",
                prefix=("select=1",),
                index=0,
                timeout_s=5.0,
                stop_event=threading.Event(),
                artifact_dir=Path(temp),
                bootstrap_fn=bootstrap,
                post_action_fn=post_action,
                validate_fn=lambda *args, **kwargs: [],
            )

        self.assertEqual(outcome["outcome"], "PROMPT_NOT_REACHED")
        self.assertFalse(outcome["target_reached"])


if __name__ == "__main__":
    unittest.main()
