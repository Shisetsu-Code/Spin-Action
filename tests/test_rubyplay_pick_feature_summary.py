from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.feature_sessions import FEATURE_COMPLETE
from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers.rubyplay.feature_sessions import build_rubyplay_feature_sessions


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class RubyPlayPickFeatureSummaryTests(unittest.TestCase):
    def test_repeated_pick_requests_share_one_proven_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt_dir = root / "PURCHASE_PICK" / "attempt-00001"
            attempt_dir.mkdir(parents=True)
            _write(attempt_dir / "request.json", {"action": "buy_feature", "buy_feature_type": "pick"})
            _write(attempt_dir / "response.json", {"data": {"next_action": "pick"}})
            for step, index, next_action in (
                (2, 0, "pick"),
                (3, 1, "pick"),
                (4, 2, "spin"),
            ):
                _write(attempt_dir / f"step-{step:03d}-request.json", {"action": "pick", "index": index})
                _write(attempt_dir / f"step-{step:03d}-response.json", {"data": {"next_action": next_action}})

            result = GameTestResult(
                provider="rubyplay",
                slug="synthetic-pick",
                game_name="Synthetic Pick",
                game_url="https://example.invalid/pick",
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
                        wire_steps=4,
                        na="spin",
                        artifact_dir=str(attempt_dir),
                    )
                ],
                discovered_modes=[
                    {
                        "id": "PURCHASE_PICK__PICK_INDEX_DOMAIN",
                        "kind": "INDEXED_CHOICE",
                        "parent": "PURCHASE_PICK",
                        "wire_command": "pick",
                        "coverage_required": True,
                        "required_options": ["0", "1", "2"],
                        "covered_options": ["0", "1", "2"],
                        "required_samples": 1,
                        "sample_counts": {"0": 1, "1": 1, "2": 1},
                        "domain_authority": "isolated-live-server-rejection-window",
                        "boundary_index": 3,
                        "boundary_confirmations": 2,
                        "rejection_span": 2,
                    }
                ],
            )

            report = build_rubyplay_feature_sessions(result)

        session = report["sessions"][0]
        self.assertEqual(session["state"], FEATURE_COMPLETE)
        self.assertEqual(session["totals"]["choices"], 1)
        self.assertEqual(session["choices"][0]["command"], "pick")
        self.assertEqual(session["choices"][0]["required_options"], ["0", "1", "2"])
        picks = [
            row for row in session["transitions"]
            if row.get("provider_action") == "pick"
        ]
        self.assertEqual([row["selected"] for row in picks], ["0", "1", "2"])


if __name__ == "__main__":
    unittest.main()
