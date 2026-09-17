from __future__ import annotations

import unittest

from tester_spin.providers.rubyplay.choice_domains import (
    classify_probe_failure,
    probe_contiguous_index_domain,
    prove_contiguous_index_domain,
    rubyplay_choice_domain_is_proven,
)


class RubyPlayChoiceDomainProofTests(unittest.TestCase):
    def test_finite_required_and_covered_without_authority_is_not_proven(self) -> None:
        self.assertFalse(
            rubyplay_choice_domain_is_proven(
                {
                    "kind": "INDEXED_CHOICE",
                    "required_options": ["0", "1"],
                    "covered_options": ["0", "1"],
                }
            )
        )

    def test_rejection_window_authority_with_full_evidence_is_proven(self) -> None:
        self.assertTrue(
            rubyplay_choice_domain_is_proven(
                {
                    "kind": "INDEXED_CHOICE",
                    "domain_authority": "isolated-live-server-rejection-window",
                    "required_options": ["0", "1"],
                    "covered_options": ["0", "1"],
                    "boundary_index": 2,
                    "boundary_confirmations": 2,
                    "rejection_span": 2,
                }
            )
        )

    def test_boundary_contradicted_by_covered_index_is_not_proven(self) -> None:
        self.assertFalse(
            rubyplay_choice_domain_is_proven(
                {
                    "kind": "INDEXED_CHOICE",
                    "domain_authority": "isolated-live-server-rejection-window",
                    "required_options": ["0", "1"],
                    "covered_options": ["0", "1", "2"],
                    "boundary_index": 2,
                    "boundary_confirmations": 2,
                    "rejection_span": 2,
                }
            )
        )

    def test_boundary_contradicted_by_observed_index_is_not_proven(self) -> None:
        self.assertFalse(
            rubyplay_choice_domain_is_proven(
                {
                    "kind": "INDEXED_CHOICE",
                    "domain_authority": "isolated-live-server-rejection-window",
                    "required_options": ["0", "1"],
                    "covered_options": ["0", "1"],
                    "observed_indices": [0, 2],
                    "boundary_index": 2,
                    "boundary_confirmations": 2,
                    "rejection_span": 2,
                }
            )
        )

    def test_server_authority_without_full_rejection_window_is_not_proven(self) -> None:
        self.assertFalse(
            rubyplay_choice_domain_is_proven(
                {
                    "kind": "INDEXED_CHOICE",
                    "domain_authority": "isolated-live-server-rejection-window",
                    "required_options": ["0", "1"],
                    "covered_options": ["0", "1"],
                    "boundary_index": 2,
                    "boundary_confirmations": 2,
                    "rejection_span": 1,
                }
            )
        )

    def test_action_name_only_error_is_not_a_domain_boundary(self) -> None:
        failure = classify_probe_failure(
            ValueError("RubyPlay select: status='error'."),
            last_payload={
                "status": "error",
                "topic": "gameserver/select",
                "error": "select unavailable in current state",
            },
            action="select",
        )
        self.assertEqual(failure["outcome"], "PROTOCOL_ERROR")

    def test_explicit_invalid_index_is_a_semantic_boundary_candidate(self) -> None:
        failure = classify_probe_failure(
            ValueError("RubyPlay select: status='error'."),
            last_payload={
                "status": "error",
                "topic": "gameserver/select",
                "error": "invalid index",
            },
            action="select",
        )
        self.assertEqual(failure["outcome"], "SEMANTIC_REJECTION")

    def test_out_of_range_choice_is_a_semantic_boundary_candidate(self) -> None:
        failure = classify_probe_failure(
            ValueError("RubyPlay pick: status='error'."),
            last_payload={
                "status": "error",
                "topic": "gameserver/pick",
                "message": "choice index out of range",
            },
            action="pick",
        )
        self.assertEqual(failure["outcome"], "SEMANTIC_REJECTION")

    def test_terminal_prefix_plus_repeated_semantic_rejection_proves_finite_domain(self) -> None:
        proof = prove_contiguous_index_domain(
            [
                {"index": 0, "outcome": "TERMINAL"},
                {"index": 1, "outcome": "TERMINAL"},
                {"index": 2, "outcome": "SEMANTIC_REJECTION", "provider_status": "error"},
                {"index": 2, "outcome": "SEMANTIC_REJECTION", "provider_status": "error"},
                {"index": 3, "outcome": "SEMANTIC_REJECTION", "provider_status": "error"},
                {"index": 3, "outcome": "SEMANTIC_REJECTION", "provider_status": "error"},
            ]
        )
        self.assertEqual(proof["state"], "PROVEN")
        self.assertEqual(proof["required_options"], ["0", "1"])
        self.assertEqual(proof["covered_options"], ["0", "1"])
        self.assertEqual(proof["boundary_index"], 2)
        self.assertEqual(proof["boundary_confirmations"], 2)
        self.assertEqual(proof["rejection_span"], 2)

    def test_rejected_boundary_with_valid_successor_is_unresolved(self) -> None:
        proof = prove_contiguous_index_domain(
            [
                {"index": 0, "outcome": "TERMINAL"},
                {"index": 1, "outcome": "TERMINAL"},
                {"index": 2, "outcome": "SEMANTIC_REJECTION"},
                {"index": 2, "outcome": "SEMANTIC_REJECTION"},
                {"index": 3, "outcome": "TERMINAL"},
            ]
        )
        self.assertEqual(proof["state"], "UNRESOLVED")

    def test_single_semantic_rejection_does_not_prove_boundary(self) -> None:
        proof = prove_contiguous_index_domain(
            [
                {"index": 0, "outcome": "TERMINAL"},
                {"index": 1, "outcome": "SEMANTIC_REJECTION"},
            ]
        )
        self.assertEqual(proof["state"], "UNRESOLVED")

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
                {"index": 3, "outcome": "SEMANTIC_REJECTION"},
            ]
        )
        self.assertEqual(proof["state"], "UNRESOLVED")

    def test_rejection_at_zero_does_not_invent_empty_domain(self) -> None:
        proof = prove_contiguous_index_domain(
            [
                {"index": 0, "outcome": "SEMANTIC_REJECTION"},
                {"index": 0, "outcome": "SEMANTIC_REJECTION"},
            ]
        )
        self.assertEqual(proof["state"], "UNRESOLVED")
        self.assertEqual(proof["required_options"], ["DOMAIN_UNRESOLVED"])

    def test_bounded_probe_repeats_boundary_rejection_then_stops(self) -> None:
        called = []

        def probe(index: int):
            called.append(index)
            return {
                "index": index,
                "outcome": (
                    "SEMANTIC_REJECTION"
                    if index in {3, 4}
                    else "TERMINAL"
                ),
            }

        proof = probe_contiguous_index_domain(probe, max_index=16)

        self.assertEqual(called, [0, 1, 2, 3, 3, 4, 4])
        self.assertEqual(proof["state"], "PROVEN")
        self.assertEqual(proof["required_options"], ["0", "1", "2"])

    def test_bounded_probe_stops_immediately_on_inconclusive_failure(self) -> None:
        called = []

        def probe(index: int):
            called.append(index)
            if index == 1:
                return {"index": index, "outcome": "TRANSPORT_ERROR"}
            return {"index": index, "outcome": "TERMINAL"}

        proof = probe_contiguous_index_domain(probe, max_index=16)

        self.assertEqual(called, [0, 1])
        self.assertEqual(proof["state"], "UNRESOLVED")

    def test_boundary_confirmation_mismatch_is_unresolved(self) -> None:
        outcomes = iter(
            [
                {"index": 0, "outcome": "TERMINAL"},
                {"index": 1, "outcome": "SEMANTIC_REJECTION"},
                {"index": 1, "outcome": "TRANSPORT_ERROR"},
            ]
        )

        proof = probe_contiguous_index_domain(lambda _index: next(outcomes), max_index=8)
        self.assertEqual(proof["state"], "UNRESOLVED")

    def test_probe_guard_exhaustion_never_promotes_unbounded_domain(self) -> None:
        proof = probe_contiguous_index_domain(
            lambda index: {"index": index, "outcome": "TERMINAL"},
            max_index=3,
        )
        self.assertEqual(proof["state"], "UNRESOLVED")
        self.assertEqual(proof["covered_options"], ["0", "1", "2", "3"])


if __name__ == "__main__":
    unittest.main()
