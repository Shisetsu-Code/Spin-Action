from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.purchase_retry_manifest import (
    build_retry_manifest,
    rows_from_campaign_summary,
    write_retry_manifest,
)
from tester_spin.purchase_coverage import (
    PURCHASE_COMPLETE,
    PURCHASE_FAILED,
    PURCHASE_UNKNOWN,
)


class PurchaseRetryManifestTests(unittest.TestCase):
    def test_manifest_contains_only_failed_or_unknown_games_and_pending_options(self) -> None:
        rows = [
            {
                "game": {"provider": "fake", "slug": "complete-game"},
                "coverage": {
                    "state": PURCHASE_COMPLETE,
                    "reason": "",
                    "options": [
                        {
                            "purchase_id": "P-DONE",
                            "executable": True,
                            "wire_contract_state": "PROVEN",
                            "execution_state": "COMPLETE",
                            "terminal": True,
                            "reason": "",
                        }
                    ],
                },
            },
            {
                "game": {"provider": "fake", "slug": "unknown-game"},
                "runtime_error": "",
                "coverage": {
                    "state": PURCHASE_UNKNOWN,
                    "reason": "terminality unresolved",
                    "options": [
                        {
                            "purchase_id": "P1",
                            "executable": True,
                            "wire_contract_state": "PROVEN",
                            "execution_state": "COMPLETE",
                            "terminal": True,
                            "reason": "",
                        },
                        {
                            "purchase_id": "P2",
                            "executable": True,
                            "wire_contract_state": "PROVEN",
                            "execution_state": "UNKNOWN",
                            "terminal": False,
                            "reason": "continuation domain unresolved",
                        },
                    ],
                },
            },
            {
                "game": {"provider": "fake", "slug": "failed-game"},
                "runtime_error": "wire response invalid",
                "coverage": {
                    "state": PURCHASE_FAILED,
                    "reason": "",
                    "options": [
                        {
                            "purchase_id": "B1",
                            "executable": True,
                            "wire_contract_state": "PROVEN",
                            "execution_state": "FAILED",
                            "terminal": False,
                            "reason": "provider rejected exact selector",
                        }
                    ],
                },
            },
        ]

        manifest = build_retry_manifest(rows)

        self.assertEqual(manifest["schema"], "tester-spin/purchase-retry-manifest/v1")
        self.assertEqual(manifest["count"], 2)
        self.assertEqual(
            [(item["provider"], item["slug"], item["state"]) for item in manifest["entries"]],
            [
                ("fake", "unknown-game", PURCHASE_UNKNOWN),
                ("fake", "failed-game", PURCHASE_FAILED),
            ],
        )

        unknown = manifest["entries"][0]
        self.assertEqual(unknown["reason"], "terminality unresolved")
        self.assertEqual([item["purchase_id"] for item in unknown["pending_options"]], ["P2"])
        self.assertEqual(unknown["pending_options"][0]["execution_state"], "UNKNOWN")
        self.assertEqual(unknown["pending_options"][0]["reason"], "continuation domain unresolved")

        failed = manifest["entries"][1]
        self.assertEqual(failed["reason"], "wire response invalid")
        self.assertEqual([item["purchase_id"] for item in failed["pending_options"]], ["B1"])
        self.assertEqual(failed["pending_options"][0]["execution_state"], "FAILED")

    def test_manifest_falls_back_to_coverage_identity_and_runtime_reason(self) -> None:
        manifest = build_retry_manifest(
            [
                {
                    "game": {},
                    "runtime_error": "launcher HTTP 403",
                    "coverage": {
                        "provider": "redtiger",
                        "game_slug": "blocked-game",
                        "state": PURCHASE_UNKNOWN,
                        "reason": "",
                        "options": [],
                    },
                }
            ]
        )

        self.assertEqual(manifest["count"], 1)
        entry = manifest["entries"][0]
        self.assertEqual(entry["provider"], "redtiger")
        self.assertEqual(entry["slug"], "blocked-game")
        self.assertEqual(entry["reason"], "launcher HTTP 403")
        self.assertEqual(entry["pending_options"], [])

    def test_reporter_flattens_campaign_results_and_writes_manifest(self) -> None:
        summary = {
            "provider_summaries": [
                {
                    "provider": "fake",
                    "results": [
                        {
                            "game": {"provider": "fake", "slug": "u-1"},
                            "runtime_error": "",
                            "coverage": {
                                "provider": "fake",
                                "game_slug": "u-1",
                                "state": PURCHASE_UNKNOWN,
                                "reason": "needs retry",
                                "options": [],
                            },
                        }
                    ],
                }
            ]
        }
        self.assertEqual(len(rows_from_campaign_summary(summary)), 1)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "purchase-campaign.json").write_text(
                json.dumps(summary),
                encoding="utf-8",
            )
            manifest = write_retry_manifest(root)
            stored = json.loads((root / "purchase-retry-manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest, stored)
        self.assertEqual(stored["count"], 1)
        self.assertEqual(stored["entries"][0]["slug"], "u-1")

    def test_retry_reasons_redact_urls_and_secret_like_values(self) -> None:
        manifest = build_retry_manifest(
            [
                {
                    "game": {"provider": "fake", "slug": "secret-safe"},
                    "runtime_error": "",
                    "coverage": {
                        "state": PURCHASE_UNKNOWN,
                        "reason": "GET https://example.test/demo?token=abc token=abc123 password=hunter2",
                        "options": [],
                    },
                }
            ]
        )
        reason = manifest["entries"][0]["reason"]
        self.assertNotIn("https://example.test", reason)
        self.assertNotIn("abc123", reason)
        self.assertNotIn("hunter2", reason)
        self.assertIn("<url>", reason)
        self.assertIn("<redacted>", reason)


if __name__ == "__main__":
    unittest.main()
