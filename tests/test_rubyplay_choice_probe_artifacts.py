from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.rubyplay.choice_probe import expand_rubyplay_index_domains


class _Provider:
    pass


class RubyPlayChoiceProbeArtifactTests(unittest.TestCase):
    def test_repeated_boundary_probe_uses_distinct_attempt_directories(self) -> None:
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
            seen: list[tuple[int, Path]] = []

            def replay(*args, index: int, artifact_dir: Path, **kwargs):
                seen.append((index, Path(artifact_dir)))
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

        boundary_paths = [path for index, path in seen if index == 2]
        self.assertEqual(len(boundary_paths), 2)
        self.assertNotEqual(boundary_paths[0], boundary_paths[1])
        self.assertEqual(boundary_paths[0].name, "attempt-001")
        self.assertEqual(boundary_paths[1].name, "attempt-002")
        self.assertEqual(boundary_paths[0].parent.name, "index-002")
        self.assertEqual(boundary_paths[1].parent.name, "index-002")


if __name__ == "__main__":
    unittest.main()
