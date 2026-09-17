from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.rubyplay.choice_probe import expand_rubyplay_index_domains


class _Provider:
    pass


class RubyPlaySelectPathExpansionTests(unittest.TestCase):
    def test_each_select_prefix_gets_independent_finite_domain(self) -> None:
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
                boundary = 2 if not prefix else 1
                return {
                    "index": index,
                    "prefix": list(prefix),
                    "outcome": (
                        "SEMANTIC_REJECTION"
                        if index in {boundary, boundary + 1}
                        else "TERMINAL"
                    ),
                    "target_reached": True,
                }

            expand_rubyplay_index_domains(
                _Provider(),
                game,
                result,
                observed={
                    ("PURCHASE_SELECT", "select", ()): {1},
                    ("PURCHASE_SELECT", "select", ("select=1",)): {0},
                },
                timeout_s=5.0,
                stop_event=threading.Event(),
                progress=lambda _message: None,
                replay_fn=replay,
                max_index=8,
            )

        modes = [
            row for row in result.discovered_modes
            if row.get("kind") == "INDEXED_CHOICE"
        ]
        self.assertEqual(len(modes), 2)
        by_prefix = {tuple(row.get("prefix") or []): row for row in modes}
        self.assertEqual(by_prefix[()]["required_options"], ["0", "1"])
        self.assertEqual(
            by_prefix[("select=1",)]["required_options"],
            ["0"],
        )
        self.assertNotEqual(by_prefix[()]["id"], by_prefix[("select=1",)]["id"])
        self.assertTrue(any(prefix == ("select=1",) and index == 0 for prefix, index in calls))


if __name__ == "__main__":
    unittest.main()
