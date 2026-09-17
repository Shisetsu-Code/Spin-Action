from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.rubyplay.choice_probe import (
    expand_rubyplay_index_domains,
    replay_index_probe,
)


class _Session:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Provider:
    def _new_session(self):
        return _Session()


class RubyPlayChoiceProbeTests(unittest.TestCase):
    def test_purchase_replay_forces_target_index_and_runs_to_terminal(self) -> None:
        runtime = SimpleNamespace(
            session=_Session(),
            default_bet=10,
            next_action="spin",
            client_profile=SimpleNamespace(wager=10.0, buy_feature_multiplier=50.0),
        )
        calls = []

        def bootstrap(session, url, **kwargs):
            runtime.session = session
            return runtime

        def post_action(rt, action, **kwargs):
            calls.append((action, kwargs.get("action_index")))
            if action == "buy_feature":
                rt.next_action = "select"
            elif action == "select":
                self.assertEqual(kwargs.get("action_index"), 1)
                rt.next_action = "freespin"
            elif action == "freespin":
                rt.next_action = "spin"
            return SimpleNamespace(status_code=200), {"action": action}, {"status": "ok", "data": {"next_action": rt.next_action}}, 0

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
                index=1,
                timeout_s=5.0,
                stop_event=threading.Event(),
                artifact_dir=Path(temp),
                bootstrap_fn=bootstrap,
                post_action_fn=post_action,
                validate_fn=lambda *args, **kwargs: [],
            )

        self.assertEqual(outcome["outcome"], "TERMINAL")
        self.assertTrue(outcome["target_reached"])
        self.assertEqual(calls, [("buy_feature", None), ("select", 1), ("freespin", None)])

    def test_pick_replay_never_reuses_forced_pick_index(self) -> None:
        runtime = SimpleNamespace(
            session=_Session(),
            default_bet=10,
            next_action="spin",
            client_profile=SimpleNamespace(wager=10.0, buy_feature_multiplier=50.0),
        )
        picked = []

        def bootstrap(session, url, **kwargs):
            runtime.session = session
            return runtime

        def post_action(rt, action, **kwargs):
            if action == "buy_feature":
                rt.next_action = "pick"
            elif action == "pick":
                picked.append(kwargs.get("action_index"))
                rt.next_action = "pick" if len(picked) == 1 else "spin"
            return SimpleNamespace(status_code=200), {"action": action}, {"status": "ok", "data": {"next_action": rt.next_action}}, 0

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
                    "id": "PURCHASE_PICK",
                    "kind": "PURCHASE",
                    "buy_feature_type": "pick",
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
                parent_mode="PURCHASE_PICK",
                action="pick",
                index=3,
                timeout_s=5.0,
                stop_event=threading.Event(),
                artifact_dir=Path(temp),
                bootstrap_fn=bootstrap,
                post_action_fn=post_action,
                validate_fn=lambda *args, **kwargs: [],
            )

        self.assertEqual(outcome["outcome"], "TERMINAL")
        self.assertEqual(picked, [3, 0])
        self.assertEqual(len(set(picked)), 2)

    def test_expand_promotes_only_proven_parent_scoped_domain(self) -> None:
        result = GameTestResult(
            provider="rubyplay",
            slug="g",
            game_name="G",
            game_url="https://example.invalid/g",
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
            discovered_modes=[{"id": "PURCHASE_SELECT", "kind": "PURCHASE"}],
            run_dir="",
        )
        game = Game(provider="rubyplay", slug="g", name="G", url=result.game_url)

        def replay(*args, index: int, **kwargs):
            return {
                "index": index,
                "outcome": "SEMANTIC_REJECTION" if index == 2 else "TERMINAL",
                "target_reached": True,
            }

        expand_rubyplay_index_domains(
            _Provider(),
            game,
            result,
            observed={("PURCHASE_SELECT", "select"): {0}},
            timeout_s=5.0,
            stop_event=threading.Event(),
            progress=lambda _message: None,
            replay_fn=replay,
            max_index=8,
        )

        modes = [
            mode for mode in result.discovered_modes
            if mode.get("kind") == "INDEXED_CHOICE"
        ]
        self.assertEqual(len(modes), 1)
        self.assertEqual(modes[0]["parent"], "PURCHASE_SELECT")
        self.assertEqual(modes[0]["required_options"], ["0", "1"])
        self.assertEqual(modes[0]["covered_options"], ["0", "1"])
        self.assertEqual(modes[0]["domain_authority"], "isolated-live-server-boundary")


if __name__ == "__main__":
    unittest.main()
