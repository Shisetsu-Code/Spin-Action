from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.run_result_publisher import (
    _commit_document,
    build_result_document,
    result_document_path,
)


class RunResultPublisherTests(unittest.TestCase):
    @staticmethod
    def _result(root: Path) -> GameTestResult:
        return GameTestResult(
            provider="bgaming",
            slug="adventures-demo",
            game_name="Adventures Demo",
            game_url="https://example.invalid/game",
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="PARCIAL",
            symbol="Adventures",
            run_dir=str(root),
            attempts=[
                SpinAttempt(
                    number=1,
                    ok=True,
                    mode_id="PURCHASE_BONUS_BUY",
                    terminal=False,
                    artifact_dir=str(root / "PURCHASE_BONUS_BUY" / "attempt-001"),
                )
            ],
        )

    def test_document_embeds_textual_request_response_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "run"
            attempt = root / "PURCHASE_BONUS_BUY" / "attempt-001"
            attempt.mkdir(parents=True)
            (attempt / "step-001-request.json").write_text(
                json.dumps({"command": "spin", "options": {"purchased_feature": "bonus_buy"}}),
                encoding="utf-8",
            )
            (attempt / "step-001-response.json").write_text(
                json.dumps({"flow": {"state": "select_bonus"}, "game": {"freespin_params": {"variants": [{"name": "a"}, {"name": "b"}]}}}),
                encoding="utf-8",
            )
            (root / "path-coverage.json").write_text(
                json.dumps({"complete": False}),
                encoding="utf-8",
            )
            (root / "ignored.bin").write_bytes(b"\x00\x01")

            document = build_result_document(self._result(root), source_commit="abc123")

            self.assertEqual(document["schema"], "tester-spin/github-run-result/v1")
            self.assertEqual(document["source_commit"], "abc123")
            self.assertEqual(document["result"]["run_dir"], "run")
            self.assertIn("PURCHASE_BONUS_BUY/attempt-001/step-001-request.json", document["artifacts"])
            response = document["artifacts"]["PURCHASE_BONUS_BUY/attempt-001/step-001-response.json"]
            self.assertEqual(response["encoding"], "json")
            self.assertEqual(response["value"]["flow"]["state"], "select_bonus")
            self.assertNotIn("ignored.bin", document["artifacts"])
            self.assertEqual(result_document_path(self._result(root)), "run-results/bgaming/adventures-demo.json")

    def test_git_writer_updates_orphan_results_branch_without_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            repo = base / "repo"
            remote = base / "remote.git"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            (repo / "README.md").write_text("main\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)

            first = _commit_document(
                repo,
                "run-results/bgaming/game.json",
                '{"version": 1}',
                "first result",
            )
            self.assertTrue(first)
            content = subprocess.run(
                ["git", f"--git-dir={remote}", "show", "tester-spin-runs:run-results/bgaming/game.json"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(content, '{"version": 1}')

            second = _commit_document(
                repo,
                "run-results/bgaming/game.json",
                '{"version": 2}',
                "second result",
            )
            self.assertNotEqual(first, second)
            content = subprocess.run(
                ["git", f"--git-dir={remote}", "show", "tester-spin-runs:run-results/bgaming/game.json"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(content, '{"version": 2}')
            current = subprocess.run(
                ["git", "-C", str(repo), "branch", "--show-current"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertNotEqual(current, "tester-spin-runs")


if __name__ == "__main__":
    unittest.main()
