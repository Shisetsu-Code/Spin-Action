from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tester_spin.models import GameTestResult
from tester_spin.providers.bgaming.profile import BGamingProfile
import tester_spin.providers.bgaming_path_policy as policy
import tester_spin.providers.bgaming_paths_v2 as v2


class BGamingPathsV2Regressions(unittest.TestCase):
    def tearDown(self) -> None:
        policy.end_policy_run()

    def test_refreshed_command_options_are_merged_before_profile_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "game.json"
            policy.begin_policy_run()
            policy._LOCAL.proven_command_options = {}
            refreshed = BGamingProfile(
                family="api-v2",
                command_options={"select_bonus": {"rows": 4}},
            )
            with patch.object(policy, "_ORIGINAL_DISCOVER_PROFILE", return_value=refreshed):
                v2._profile_guard(None, {}, timeout_s=1, persisted=None)
            active = BGamingProfile(family="api-v2")
            v2._save_profile_guard(path, active)
            self.assertEqual(active.command_options, {"select_bonus": {"rows": 4}})

    def test_sparse_choice_can_be_scheduled_again_after_first_replay(self) -> None:
        policy.begin_policy_run()
        policy._LOCAL.choice_replay_counts = {}
        graph = {
            ("SPIN", "select_bonus", (), ("A", "B")): {
                "scope": "SPIN",
                "command": "select_bonus",
                "prefix": (),
                "available": ("A", "B"),
                "sample_counts": {"A": 0, "B": 0},
            }
        }
        attempted = set()
        first = v2._next_missing_choice_guard(graph, attempted, 2)
        attempted.add(first)
        second = v2._next_missing_choice_guard(graph, attempted, 2)
        attempted.add(second)
        third = v2._next_missing_choice_guard(graph, attempted, 2)
        self.assertNotEqual(first, second)
        self.assertEqual(third, first)

    def test_repeated_choice_replay_uses_new_evidence_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            target = root / "branch"
            source.mkdir()
            target.mkdir()
            (source / "new.json").write_text("{}", encoding="utf-8")
            (target / "old.json").write_text("{}", encoding="utf-8")
            result = GameTestResult(
                provider="bgaming",
                slug="synthetic",
                game_name="Synthetic",
                game_url="https://example.invalid",
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="OK",
                run_dir=str(source),
            )
            policy.begin_policy_run()
            v2._move_run_guard(result, target)
            self.assertTrue((target / "old.json").is_file())
            self.assertTrue((root / "branch-sample-002" / "new.json").is_file())
            self.assertEqual(Path(result.run_dir), root / "branch-sample-002")


if __name__ == "__main__":
    unittest.main()
