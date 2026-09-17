from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.rubyplay.choice_probe import expand_rubyplay_index_domains


class _Provider:
    pass


class RubyPlayChoicePromptRetryTests(unittest.TestCase):
    def test_prompt_not_reached_is_retried_before_domain_is_abandoned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = GameTestResult(
                provider="rubyplay",
                slug="g",
                game_name="G",
                game_url="https://example.invalid/g",
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="OK",
                run_dir=str(root),
                discovered_modes=[{"id": "SPIN", "kind": "SPIN"}],
            )
            game = Game(provider="rubyplay", slug="g", name="G", url=result.game_url)
            calls: dict[int, int] = {}

            def replay(*args, index: int, **kwargs):
                calls[index] = calls.get(index, 0) + 1
                if index == 0 and calls[index] < 3:
                    return {
                        "index": index,
                        "outcome": "PROMPT_NOT_REACHED",
                        "target_reached": False,
                    }
                if index == 1:
                    return {
                        "index": index,
                        "outcome": "SEMANTIC_REJECTION",
                        "target_reached": True,
                    }
                return {
                    "index": index,
                    "outcome": "TERMINAL",
                    "target_reached": True,
                }

            expand_rubyplay_index_domains(
                _Provider(),
                game,
                result,
                observed={("SPIN", "select"): {0}},
                timeout_s=5.0,
                stop_event=threading.Event(),
                progress=lambda _message: None,
                replay_fn=replay,
                max_index=8,
                prompt_retries=3,
            )

        self.assertEqual(calls[0], 3)
        self.assertEqual(calls[1], 2)
        modes = [row for row in result.discovered_modes if row.get("kind") == "INDEXED_CHOICE"]
        self.assertEqual(len(modes), 1)
        self.assertEqual(modes[0]["parent"], "SPIN")
        self.assertEqual(modes[0]["required_options"], ["0"])

    def test_transport_failure_is_not_retried_as_prompt_absence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = GameTestResult(
                provider="rubyplay",
                slug="g",
                game_name="G",
                game_url="https://example.invalid/g",
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="OK",
                run_dir=str(root),
                discovered_modes=[{"id": "SPIN", "kind": "SPIN"}],
            )
            game = Game(provider="rubyplay", slug="g", name="G", url=result.game_url)
            calls = 0

            def replay(*args, index: int, **kwargs):
                nonlocal calls
                calls += 1
                return {"index": index, "outcome": "TRANSPORT_ERROR"}

            expand_rubyplay_index_domains(
                _Provider(),
                game,
                result,
                observed={("SPIN", "select"): {0}},
                timeout_s=5.0,
                stop_event=threading.Event(),
                progress=lambda _message: None,
                replay_fn=replay,
                max_index=8,
                prompt_retries=5,
            )

        self.assertEqual(calls, 1)
        self.assertFalse(any(row.get("kind") == "INDEXED_CHOICE" for row in result.discovered_modes))


if __name__ == "__main__":
    unittest.main()
