from __future__ import annotations

import json
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


class _JsonResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self):
        return self.payload


class _RejectSession(_Session):
    def post(self, url, *, json, timeout):
        return _JsonResponse(
            {
                "status": "error",
                "topic": "gameserver/select",
                "error": "invalid index",
            }
        )


class _Provider:
    def _new_session(self):
        return _Session()


class _RejectProvider:
    def _new_session(self):
        return _RejectSession()


class RubyPlayChoiceProbeTests(unittest.TestCase):
    @staticmethod
    def _purchase_result(mode_id: str, feature_type: str) -> GameTestResult:
        return GameTestResult(
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
                    "id": mode_id,
                    "kind": "PURCHASE",
                    "buy_feature_type": feature_type,
                    "default_price": 5000,
                }
            ],
        )

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

        result = self._purchase_result("PURCHASE_SELECT", "select")
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

        result = self._purchase_result("PURCHASE_PICK", "pick")
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

    def test_semantic_boundary_rejection_persists_request_and_response(self) -> None:
        runtime = SimpleNamespace(
            session=_RejectSession(),
            default_bet=10,
            next_action="spin",
            client_profile=SimpleNamespace(wager=10.0, buy_feature_multiplier=50.0),
        )

        def bootstrap(session, url, **kwargs):
            runtime.session = session
            return runtime

        def post_action(rt, action, **kwargs):
            if action == "buy_feature":
                rt.next_action = "select"
                return (
                    SimpleNamespace(status_code=200),
                    {"action": action, "key": "secret"},
                    {"status": "ok", "data": {"next_action": "select"}},
                    0,
                )
            if action == "select":
                rt.session.post(
                    "https://example.invalid/gameserver/demo",
                    json={
                        "action": "select",
                        "index": kwargs.get("action_index"),
                        "key": "secret",
                    },
                    timeout=kwargs["timeout_s"],
                )
                raise ValueError("RubyPlay select: status='error'.")
            raise AssertionError(action)

        result = self._purchase_result("PURCHASE_SELECT", "select")
        game = Game(provider="rubyplay", slug="g", name="G", url=result.game_url)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outcome = replay_index_probe(
                _RejectProvider(),
                game,
                result,
                parent_mode="PURCHASE_SELECT",
                action="select",
                index=2,
                timeout_s=5.0,
                stop_event=threading.Event(),
                artifact_dir=root,
                bootstrap_fn=bootstrap,
                post_action_fn=post_action,
                validate_fn=lambda *args, **kwargs: [],
            )
            failure_request = json.loads((root / "failure-request.json").read_text(encoding="utf-8"))
            failure_response = json.loads((root / "failure-response.json").read_text(encoding="utf-8"))
            failure = json.loads((root / "failure.json").read_text(encoding="utf-8"))

        self.assertEqual(outcome["outcome"], "SEMANTIC_REJECTION")
        self.assertEqual(failure_request["index"], 2)
        self.assertEqual(failure_request["key"], "<redacted-session-key>")
        self.assertEqual(failure_response["error"], "invalid index")
        self.assertEqual(failure["outcome"], "SEMANTIC_REJECTION")

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
        self.assertEqual(modes[0]["boundary_confirmations"], 2)


if __name__ == "__main__":
    unittest.main()
