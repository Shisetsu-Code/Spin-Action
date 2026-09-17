from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers.rubyplay.choice_probe import observed_index_prompts


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class RubyPlaySelectPromptPathTests(unittest.TestCase):
    def test_repeated_selects_are_separate_prompt_points_by_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt_dir = root / "PURCHASE_SELECT" / "attempt-00001"
            attempt_dir.mkdir(parents=True)
            _write(attempt_dir / "request.json", {"action": "buy_feature"})
            _write(attempt_dir / "step-002-request.json", {"action": "select", "index": 1})
            _write(attempt_dir / "step-003-request.json", {"action": "freespin"})
            _write(attempt_dir / "step-004-request.json", {"action": "select", "index": 0})
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
                attempts=[
                    SpinAttempt(
                        number=1,
                        ok=True,
                        mode_id="PURCHASE_SELECT",
                        mode_kind="PURCHASE",
                        terminal=True,
                        artifact_dir=str(attempt_dir),
                    )
                ],
            )

            prompts = observed_index_prompts(result)

        self.assertEqual(prompts[("PURCHASE_SELECT", "select", ())], {1})
        self.assertEqual(
            prompts[("PURCHASE_SELECT", "select", ("select=1",))],
            {0},
        )
        self.assertEqual(len(prompts), 2)

    def test_mixed_indexed_actions_preserve_action_in_select_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt_dir = root / "SPIN" / "attempt-00001"
            attempt_dir.mkdir(parents=True)
            _write(attempt_dir / "request.json", {"action": "spin"})
            _write(attempt_dir / "step-002-request.json", {"action": "pick", "index": 2})
            _write(attempt_dir / "step-003-request.json", {"action": "select", "index": 1})
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
                attempts=[
                    SpinAttempt(
                        number=1,
                        ok=True,
                        mode_id="SPIN",
                        mode_kind="SPIN",
                        terminal=True,
                        artifact_dir=str(attempt_dir),
                    )
                ],
            )

            prompts = observed_index_prompts(result)

        self.assertEqual(prompts[("SPIN", "pick", ())], {2})
        self.assertEqual(prompts[("SPIN", "select", ("pick=2",))], {1})

    def test_repeated_picks_share_one_domain_point(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt_dir = root / "PURCHASE_PICK" / "attempt-00001"
            attempt_dir.mkdir(parents=True)
            _write(attempt_dir / "request.json", {"action": "buy_feature"})
            for step, index in ((2, 0), (3, 1), (4, 2)):
                _write(attempt_dir / f"step-{step:03d}-request.json", {"action": "pick", "index": index})
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
                attempts=[
                    SpinAttempt(
                        number=1,
                        ok=True,
                        mode_id="PURCHASE_PICK",
                        mode_kind="PURCHASE",
                        terminal=True,
                        artifact_dir=str(attempt_dir),
                    )
                ],
            )

            prompts = observed_index_prompts(result)

        self.assertEqual(prompts, {("PURCHASE_PICK", "pick", ()): {0, 1, 2}})


if __name__ == "__main__":
    unittest.main()
