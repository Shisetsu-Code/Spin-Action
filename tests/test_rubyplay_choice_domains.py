from __future__ import annotations

import unittest

from tester_spin.providers.rubyplay.choice_domains import prove_contiguous_index_domain


class RubyPlayChoiceDomainProofTests(unittest.TestCase):
    def test_terminal_prefix_plus_semantic_rejection_proves_finite_domain(self) -> None:
        proof = prove_contiguous_index_domain(
            [
                {"index": 0, "outcome": "TERMINAL"},
                {"index": 1, "outcome": "TERMINAL"},
                {"index": 2, "outcome": "SEMANTIC_REJECTION", "provider_status": "error"},
            ]
        )
        self.assertEqual(proof["state"], "PROVEN")
        self.assertEqual(proof["required_options"], ["0", "1"])
        self.assertEqual(proof["covered_options"], ["0", "1"])
        self.assertEqual(proof["boundary_index"], 2)

    def test_transport_failure_never_proves_boundary(self) -> None:
        proof = prove_contiguous_index_domain(
            [
                {"index": 0, "outcome": "TERMINAL"},
                {"index": 1, "outcome": "TRANSPORT_ERROR"},
            ]
        )
        self.assertEqual(proof["state"], "UNRESOLVED")
        self.assertEqual(proof["required_options"], ["DOMAIN_UNRESOLVED"])

    def test_nonterminal_acceptance_never_counts_as_covered_option(self) -> None:
        proof = prove_contiguous_index_domain(
            [
                {"index": 0, "outcome": "TERMINAL"},
                {"index": 1, "outcome": "NONTERMINAL"},
                {"index": 2, "outcome": "SEMANTIC_REJECTION"},
            ]
        )
        self.assertEqual(proof["state"], "UNRESOLVED")
        self.assertEqual(proof["covered_options"], ["0"])

    def test_missing_index_in_probe_sequence_never_closes_domain(self) -> None:
        proof = prove_contiguous_index_domain(
            [
                {"index": 0, "outcome": "TERMINAL"},
                {"index": 2, "outcome": "TERMINAL"},
                {"index": 3, "outcome": "SEMANTIC_REJECTION"},
            ]
        )
        self.assertEqual(proof["state"], "UNRESOLVED")

    def test_rejection_at_zero_does_not_invent_empty_domain(self) -> None:
        proof = prove_contiguous_index_domain(
            [{"index": 0, "outcome": "SEMANTIC_REJECTION"}]
        )
        self.assertEqual(proof["state"], "UNRESOLVED")
        self.assertEqual(proof["required_options"], ["DOMAIN_UNRESOLVED"])


if __name__ == "__main__":
    unittest.main()
