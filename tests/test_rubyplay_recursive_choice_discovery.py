from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.rubyplay.choice_probe import expand_rubyplay_index_domains


class _Provider:
    pass


class RubyPlayRecursiveChoiceDiscoveryTests(unittest.TestCase):
    def test_terminal_sibling_replay_discovers_and_closes_nested_select_prompt(self) -> None:
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
                discovered_modes=[{"id": "PURCHASE_SELECT", "kind": "PURCHASE"}],
            )
            game = Game(provider="rubyplay", slug="g", name="G", url=result.game_url)
            calls: list[tuple[tuple[str, ...], int]] = []

            def replay(*args, prefix=(), index: int, **kwargs):
                prefix = tuple(prefix)
                calls.append((prefix, index))
                if not prefix:
                    if index == 1:
                        return {"index": index, "prefix": [], "outcome": "SEMANTIC_REJECTION"}
                    return {
                        "index": index,
                        "prefix": [],
                        "outcome": "TERMINAL",
                        "target_reached": True,
                        "indexed_trace": [
                            {"action": "select", "prefix": [], "selected": "0"},
                            {
                                "action": "select",
                                "prefix": ["select=0"],
                                "selected": "0",
                            },
                        ],
                    }
                if prefix == ("select=0",):
                    if index == 1:
                        return {
                            "index": index,
                            "prefix": list(prefix),
                            "outcome": "SEMANTIC_REJECTION",
                        }
                    return {
                        "index": index,
                        "prefix": list(prefix),
                        "outcome": "TERMINAL",
                        "target_reached": True,
                        "indexed_trace": [
                            {"action": "select", "prefix": [], "selected": "0"},
                            {
                                "action": "select",
                                "prefix": ["select=0"],
                                "selected": "0",
                            },
                        ],
                    }
                raise AssertionError(prefix)

            expand_rubyplay_index_domains(
                _Provider(),
                game,
                result,
                observed={("PURCHASE_SELECT", "select", ()): {0}},
                timeout_s=5.0,
                stop_event=threading.Event(),
                progress=lambda _message: None,
                replay_fn=replay,
                max_index=8,
            )

        modes = [row for row in result.discovered_modes if row.get("kind") == "INDEXED_CHOICE"]
        by_prefix = {tuple(row.get("prefix") or []): row for row in modes}
        self.assertEqual(by_prefix[()]["required_options"], ["0"])
        self.assertEqual(by_prefix[("select=0",)]["required_options"], ["0"])
        self.assertTrue(any(prefix == ("select=0",) for prefix, _index in calls))

    def test_nested_prompt_that_cannot_be_proven_is_persisted_unresolved(self) -> None:
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
                discovered_modes=[{"id": "PURCHASE_SELECT", "kind": "PURCHASE"}],
            )
            game = Game(provider="rubyplay", slug="g", name="G", url=result.game_url)

            def replay(*args, prefix=(), index: int, **kwargs):
                prefix = tuple(prefix)
                if not prefix:
                    if index == 1:
                        return {"index": index, "outcome": "SEMANTIC_REJECTION"}
                    return {
                        "index": index,
                        "outcome": "TERMINAL",
                        "target_reached": True,
                        "indexed_trace": [
                            {"action": "select", "prefix": [], "selected": "0"},
                            {
                                "action": "select",
                                "prefix": ["select=0"],
                                "selected": "0",
                            },
                        ],
                    }
                return {"index": index, "outcome": "TRANSPORT_ERROR"}

            expand_rubyplay_index_domains(
                _Provider(),
                game,
                result,
                observed={("PURCHASE_SELECT", "select", ()): {0}},
                timeout_s=5.0,
                stop_event=threading.Event(),
                progress=lambda _message: None,
                replay_fn=replay,
                max_index=8,
            )

        modes = [row for row in result.discovered_modes if row.get("kind") == "INDEXED_CHOICE"]
        by_prefix = {tuple(row.get("prefix") or []): row for row in modes}
        self.assertEqual(by_prefix[()]["required_options"], ["0"])
        self.assertEqual(
            by_prefix[("select=0",)]["required_options"],
            ["DOMAIN_UNRESOLVED"],
        )
        self.assertEqual(by_prefix[("select=0",)]["covered_options"], [])


if __name__ == "__main__":
    unittest.main()
