from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.providers.pragmatic_protocol import analyze_response, summarize_analysis_files


class PragmaticProtocolAnalysisTests(unittest.TestCase):
    def test_collect_state_is_understood(self) -> None:
        analysis = analyze_response({"na": "c", "tw": "12.5", "index": "5", "counter": "8"})
        self.assertEqual(analysis["state_kind"], "collect_required")
        self.assertEqual(analysis["automatic_handler"], "doCollect")
        self.assertFalse(analysis["terminal_hint"])
        self.assertIn("win", analysis["value_groups"])

    def test_bonus_states_are_understood(self) -> None:
        cases = (
            ("b", "bonus_required", "doBonus"),
            ("cb", "bonus_collect_required", "doCollectBonus"),
            ("bc", "bonus_collect_required", "doCollectBonus"),
            ("B", "bonus_required", "doBonus"),
        )
        for na, state_kind, handler in cases:
            with self.subTest(na=na):
                analysis = analyze_response({"na": na, "index": "5", "counter": "8"})
                self.assertEqual(analysis["state_kind"], state_kind)
                self.assertEqual(analysis["automatic_handler"], handler)

    def test_feature_spin_continuation_is_understood(self) -> None:
        analysis = analyze_response({"na": "s", "fs": "3", "fsmax": "10", "tw": "0"})
        self.assertEqual(analysis["state_kind"], "feature_spin_continuation")
        self.assertEqual(analysis["automatic_handler"], "doSpin")
        self.assertTrue(analysis["feature_active"])
        self.assertIn("free_spins", analysis["feature_groups"])

    def test_purchase_metadata_alone_does_not_activate_feature(self) -> None:
        analysis = analyze_response({"na": "s", "puri": "0", "purtr": "1", "tw": "0"})
        self.assertEqual(analysis["state_kind"], "terminal_or_idle")
        self.assertFalse(analysis["feature_active"])
        self.assertIn("purchase", analysis["feature_groups"])

    def test_fso_capture_is_classified_as_free_spin_option_choice(self) -> None:
        # Minimal fields taken from the uploaded real captures. We intentionally do
        # not assign an automatic handler until the exact client request is captured.
        analysis = analyze_response(
            {
                "na": "fso",
                "fs_opt_mask": "fs,m,ptm",
                "fs_opt": "15,1,1~10,1,5~5,1,10~-1,-1,-1",
                "puri": "0",
                "purtr": "1",
            }
        )
        self.assertEqual(analysis["state_kind"], "free_spin_option_required")
        self.assertEqual(analysis["automatic_handler"], "")
        self.assertFalse(analysis["feature_active"])
        self.assertIn("free_spin_options", analysis["feature_groups"])

    def test_m_capture_is_classified_as_mystery_feature_step(self) -> None:
        analysis = analyze_response(
            {
                "na": "m",
                "mb": "1",
                "psym": "1~40.00~8,10,11,14",
                "fs": "1",
                "fsmax": "10",
                "puri": "0",
            }
        )
        self.assertEqual(analysis["state_kind"], "mystery_feature_step_required")
        self.assertEqual(analysis["automatic_handler"], "")
        self.assertIn("mystery_choice", analysis["feature_groups"])

    def test_unknown_state_is_fingerprinted_instead_of_discarded(self) -> None:
        first = analyze_response({"na": "x", "mystery": "abc", "index": "10"})
        second = analyze_response({"index": "11", "mystery": "def", "na": "x"})
        self.assertEqual(first["state_kind"], "provider_state_unknown")
        self.assertEqual(first["state_signature"], second["state_signature"])
        self.assertIn("mystery", first["unclassified_keys"])

    def test_finds_explicit_action_inside_embedded_json(self) -> None:
        analysis = analyze_response(
            {
                "na": "x",
                "payload": json.dumps({"nextAction": "doMysteryStep", "value": 7}),
            }
        )
        self.assertEqual(analysis["state_kind"], "explicit_action_observed")
        actions = [item["action"] for item in analysis["explicit_actions"]]
        self.assertIn("doMysteryStep", actions)

    def test_summarizes_unknown_and_known_unhandled_signatures(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = root / "SPIN" / "attempt-0001"
            attempt.mkdir(parents=True)
            (attempt / "step-000-entry.analysis.json").write_text(
                json.dumps(analyze_response({"na": "x", "mystery": "1"})),
                encoding="utf-8",
            )
            (attempt / "step-001-fso.analysis.json").write_text(
                json.dumps(
                    analyze_response(
                        {
                            "na": "fso",
                            "fs_opt_mask": "fs,m,msk",
                            "fs_opt": "15,1,0~7,1,0",
                        }
                    )
                ),
                encoding="utf-8",
            )
            (attempt / "step-002-next.analysis.json").write_text(
                json.dumps(analyze_response({"na": "c", "tw": "2"})),
                encoding="utf-8",
            )
            summary = summarize_analysis_files(root)

        self.assertEqual(summary["responses_analyzed"], 3)
        self.assertEqual(summary["state_counts"]["provider_state_unknown"], 1)
        self.assertEqual(summary["state_counts"]["free_spin_option_required"], 1)
        self.assertEqual(len(summary["unknown_signatures"]), 2)
        self.assertEqual(summary["unknown_signatures"], summary["unhandled_signatures"])


if __name__ == "__main__":
    unittest.main()
