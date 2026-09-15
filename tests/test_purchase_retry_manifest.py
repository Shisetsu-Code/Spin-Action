from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.purchase_retry_manifest import (
    build_retry_manifest,
    provider_blockers_from_campaign_summary,
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

    def test_manifest_uses_pending_option_reason_when_game_reason_is_empty(self) -> None:
        manifest = build_retry_manifest(
            [
                {
                    "game": {"provider": "fake", "slug": "option-reason"},
                    "runtime_error": "",
                    "coverage": {
                        "state": PURCHASE_UNKNOWN,
                        "reason": "",
                        "options": [
                            {
                                "purchase_id": "P-UNKNOWN",
                                "executable": False,
                                "wire_contract_state": "UNKNOWN",
                                "execution_state": "NOT_ATTEMPTED",
                                "terminal": False,
                                "reason": "serializer not proven",
                            }
                        ],
                    },
                }
            ]
        )
        self.assertEqual(manifest["entries"][0]["reason"], "serializer not proven")

    def test_manifest_never_leaves_retry_reason_blank(self) -> None:
        manifest = build_retry_manifest(
            [
                {
                    "game": {"provider": "fake", "slug": "unexplained"},
                    "runtime_error": "",
                    "coverage": {
                        "state": PURCHASE_UNKNOWN,
                        "reason": "",
                        "options": [],
                    },
                }
            ]
        )
        self.assertTrue(manifest["entries"][0]["reason"])
        self.assertIn("purchase-coverage.json", manifest["entries"][0]["reason"])

    def test_provider_catalog_failures_are_preserved_as_retry_blockers(self) -> None:
        summary = {
            "provider_summaries": [
                {
                    "provider": "rubyplay",
                    "catalog_error": "HTTP 503 while reading official catalog",
                    "results": [],
                },
                {
                    "provider": "pragmatic",
                    "results": [],
                },
            ]
        }
        blockers = provider_blockers_from_campaign_summary(summary)
        self.assertEqual(
            blockers,
            [
                {
                    "provider": "rubyplay",
                    "state": PURCHASE_UNKNOWN,
                    "reason": "HTTP 503 while reading official catalog",
                }
            ],
        )

    def test_reporter_flattens_campaign_results_and_writes_manifest(self) -> None:
        summary = {
            "closed": False,
            "aggregate": {"overall_state": PURCHASE_UNKNOWN},
            "provider_summaries": [
                {
                    "provider": "fake",
                    "catalog_error": "catalog unavailable",
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
            ],
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
        self.assertEqual(stored["provider_blocker_count"], 1)
        self.assertEqual(stored["provider_blockers"][0]["provider"], "fake")
        self.assertFalse(stored["source_closed"])
        self.assertEqual(stored["source_overall_state"], PURCHASE_UNKNOWN)

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
