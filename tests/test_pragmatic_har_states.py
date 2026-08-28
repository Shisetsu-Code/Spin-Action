from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.providers.pragmatic_har_protocol import analyze_response, summarize_analysis_files
from tester_spin.providers.pragmatic_har_states import (
    PragmaticProvider,
    build_fs_option_request,
    build_mystery_scatter_request,
    choose_fs_option_index,
    parse_fs_options,
)


FROZEN_FS_OPT = (
    "25,1,2,2;3;5~20,1,2,3;5;8~15,1,2,5;8;10~13,1,2,8;10;15~"
    "10,1,2,10;15;30~6,1,2,15;30;40~-1,-1,2,-1"
)


class PragmaticHarStateTests(unittest.TestCase):
    def test_frozen_charms_options_match_har(self) -> None:
        options = parse_fs_options(
            {
                "na": "fso",
                "fs_opt_mask": "fs,m,ts,rm",
                "fs_opt": FROZEN_FS_OPT,
            }
        )
        self.assertEqual([item["index"] for item in options], [0, 1, 2, 3, 4, 5])
        self.assertEqual(options[0]["fields"]["fs"], "25")
        self.assertEqual(options[5]["fields"]["fs"], "6")
        self.assertEqual(options[5]["fields"]["rm"], "15;30;40")

    def test_fso_selection_cycles_and_accepts_explicit_provider_index(self) -> None:
        options = parse_fs_options(
            {"fs_opt_mask": "fs,m,ts,rm", "fs_opt": FROZEN_FS_OPT}
        )
        self.assertEqual(choose_fs_option_index(options, repetition=1), 0)
        self.assertEqual(choose_fs_option_index(options, repetition=2), 1)
        self.assertEqual(choose_fs_option_index(options, repetition=6), 5)
        self.assertEqual(choose_fs_option_index(options, repetition=7), 0)
        self.assertEqual(
            choose_fs_option_index(options, repetition=1, preferred=5),
            5,
        )

    def test_do_fs_option_request_matches_har_shape(self) -> None:
        fields = build_fs_option_request(
            symbol="vswayscharms",
            mgckey="stylename@generic~SESSION@test",
            index=31,
            counter=61,
            option_index=5,
        )
        self.assertEqual(
            fields,
            {
                "action": "doFSOption",
                "symbol": "vswayscharms",
                "ind": "5",
                "index": "31",
                "counter": "61",
                "repeat": "0",
                "mgckey": "stylename@generic~SESSION@test",
            },
        )

    def test_do_mystery_scatter_request_matches_book_of_vikings_har(self) -> None:
        fields = build_mystery_scatter_request(
            symbol="vs10bookviking",
            mgckey="stylename@generic~SESSION@test",
            index=26,
            counter=51,
            s_info="n",
        )
        self.assertEqual(
            fields,
            {
                "action": "doMysteryScatter",
                "symbol": "vs10bookviking",
                "sInfo": "n",
                "index": "26",
                "counter": "51",
                "repeat": "0",
                "mgckey": "stylename@generic~SESSION@test",
            },
        )

    def test_har_analyzer_marks_fso_as_automatic(self) -> None:
        analysis = analyze_response(
            {
                "na": "fso",
                "fs_opt_mask": "fs,m,ts,rm",
                "fs_opt": FROZEN_FS_OPT,
                "puri": "0",
                "purtr": "1",
            }
        )
        self.assertEqual(analysis["state_kind"], "free_spin_option_required")
        self.assertEqual(analysis["automatic_handler"], "doFSOption")

    def test_har_analyzer_marks_m_as_automatic(self) -> None:
        analysis = analyze_response(
            {
                "na": "m",
                "mb": "1",
                "psym": "1~4.00~1,3,12",
                "fs": "1",
                "fsmax": "10",
            }
        )
        self.assertEqual(analysis["state_kind"], "mystery_feature_step_required")
        self.assertEqual(analysis["automatic_handler"], "doMysteryScatter")

    def test_summary_does_not_report_har_automated_states_as_unhandled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = root / "PURCHASE_1" / "attempt-0001"
            attempt.mkdir(parents=True)
            (attempt / "step-000-entry.analysis.json").write_text(
                json.dumps(
                    analyze_response(
                        {
                            "na": "fso",
                            "fs_opt_mask": "fs,m,ts,rm",
                            "fs_opt": FROZEN_FS_OPT,
                        }
                    )
                ),
                encoding="utf-8",
            )
            (attempt / "step-001-mystery.analysis.json").write_text(
                json.dumps(
                    analyze_response(
                        {
                            "na": "m",
                            "mb": "1",
                            "psym": "1~4.00~1,3,12",
                            "fs": "1",
                        }
                    )
                ),
                encoding="utf-8",
            )
            summary = summarize_analysis_files(root)

        self.assertEqual(summary["unhandled_signatures"], [])
        self.assertEqual(summary["har_automated_states"]["free_spin_option_required"], "doFSOption")
        self.assertEqual(summary["har_automated_states"]["mystery_feature_step_required"], "doMysteryScatter")

    def test_fso_coverage_reads_all_attempt_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for rep, selected in ((1, 0), (2, 1), (3, 5)):
                attempt = root / "PURCHASE_1" / f"attempt-{rep:04d}"
                attempt.mkdir(parents=True)
                (attempt / "fso-selection-001.json").write_text(
                    json.dumps(
                        {
                            "option_indices": [0, 1, 2, 3, 4, 5],
                            "selected_index": selected,
                        }
                    ),
                    encoding="utf-8",
                )
            coverage = PragmaticProvider._collect_fso_coverage(root)
        self.assertEqual(coverage["PURCHASE_1"]["available"], {0, 1, 2, 3, 4, 5})
        self.assertEqual(coverage["PURCHASE_1"]["selected"], {0, 1, 5})


if __name__ == "__main__":
    unittest.main()
