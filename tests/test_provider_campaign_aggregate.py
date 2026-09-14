from __future__ import annotations

import unittest

from scripts.provider_campaign_aggregate import aggregate_campaign


def _manifest(*slugs: str, authoritative: bool = True) -> dict:
    return {
        "provider": "demo",
        "authoritative": authoritative,
        "count": len(slugs),
        "games": [
            {"slug": slug, "name": slug.title(), "url": f"https://example.invalid/{slug}"}
            for slug in slugs
        ],
    }


def _coverage(slug: str, verdict: str = "COMPLETE") -> dict:
    return {
        "game": {"slug": slug},
        "audit_verdict": verdict,
        "runtime_status": "OK" if verdict == "COMPLETE" else "PARCIAL",
    }


def _natural(slug: str, *, requested: int = 3000, successful: int = 3000, failed: int = 0, status: str = "OK") -> dict:
    return {
        "slug": slug,
        "requested_spins": requested,
        "successful_spins": successful,
        "failed_spins": failed,
        "status": status,
    }


class ProviderCampaignAggregateTests(unittest.TestCase):
    def test_complete_requires_every_catalog_game_exactly_once(self) -> None:
        result = aggregate_campaign(
            _manifest("a", "b"),
            coverage_results=[_coverage("a"), _coverage("b")],
            natural_results=[_natural("a"), _natural("b")],
            required_natural_spins=3000,
        )
        self.assertEqual(result["overall_verdict"], "COMPLETE")
        self.assertEqual(result["counts"]["catalog"], 2)
        self.assertEqual(result["counts"]["coverage_complete"], 2)
        self.assertEqual(result["counts"]["natural_complete"], 2)

    def test_non_authoritative_catalog_can_never_complete(self) -> None:
        result = aggregate_campaign(
            _manifest("a", authoritative=False),
            coverage_results=[_coverage("a")],
            natural_results=[_natural("a")],
            required_natural_spins=3000,
        )
        self.assertEqual(result["overall_verdict"], "UNKNOWN")
        self.assertTrue(result["catalog_errors"])

    def test_missing_duplicate_or_extra_coverage_blocks_campaign(self) -> None:
        result = aggregate_campaign(
            _manifest("a", "b"),
            coverage_results=[_coverage("a"), _coverage("a"), _coverage("c")],
            natural_results=[_natural("a"), _natural("b")],
            required_natural_spins=3000,
        )
        self.assertEqual(result["overall_verdict"], "ERROR")
        self.assertEqual(result["coverage"]["missing"], ["b"])
        self.assertEqual(result["coverage"]["duplicates"], ["a"])
        self.assertEqual(result["coverage"]["extras"], ["c"])

    def test_incomplete_coverage_blocks_campaign(self) -> None:
        result = aggregate_campaign(
            _manifest("a", "b"),
            coverage_results=[_coverage("a"), _coverage("b", "INCOMPLETE")],
            natural_results=[_natural("a"), _natural("b")],
            required_natural_spins=3000,
        )
        self.assertEqual(result["overall_verdict"], "INCOMPLETE")
        self.assertEqual(result["coverage"]["failed"], {"b": "INCOMPLETE"})

    def test_natural_soak_requires_full_requested_success_count(self) -> None:
        result = aggregate_campaign(
            _manifest("a", "b"),
            coverage_results=[_coverage("a"), _coverage("b")],
            natural_results=[_natural("a"), _natural("b", successful=2999, failed=1, status="PARCIAL")],
            required_natural_spins=3000,
        )
        self.assertEqual(result["overall_verdict"], "INCOMPLETE")
        self.assertIn("b", result["natural"]["failed"])

    def test_missing_natural_result_blocks_when_soak_is_required(self) -> None:
        result = aggregate_campaign(
            _manifest("a", "b"),
            coverage_results=[_coverage("a"), _coverage("b")],
            natural_results=[_natural("a")],
            required_natural_spins=3000,
        )
        self.assertEqual(result["overall_verdict"], "INCOMPLETE")
        self.assertEqual(result["natural"]["missing"], ["b"])

    def test_natural_stage_can_be_disabled_for_coverage_only_campaign(self) -> None:
        result = aggregate_campaign(
            _manifest("a"),
            coverage_results=[_coverage("a")],
            natural_results=[],
            required_natural_spins=0,
        )
        self.assertEqual(result["overall_verdict"], "COMPLETE")
        self.assertFalse(result["natural"]["required"])


if __name__ == "__main__":
    unittest.main()
