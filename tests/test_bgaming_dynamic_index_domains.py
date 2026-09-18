from __future__ import annotations

from tester_spin.providers.bgaming.dynamic_index_domains import (
    ACCEPTED,
    PROVEN,
    SEMANTIC_REJECTION,
    TRANSPORT_ERROR,
    UNRESOLVED,
    probe_contiguous_index_domain,
    prove_contiguous_index_domain,
)


def test_contiguous_domain_requires_two_fresh_boundary_rejections() -> None:
    probes = [
        {"index": 0, "outcome": ACCEPTED},
        {"index": 1, "outcome": ACCEPTED},
        {"index": 2, "outcome": ACCEPTED},
        {"index": 3, "outcome": SEMANTIC_REJECTION},
        {"index": 3, "outcome": SEMANTIC_REJECTION},
    ]

    proof = prove_contiguous_index_domain(probes)

    assert proof["state"] == PROVEN
    assert proof["required_indices"] == [0, 1, 2]
    assert proof["covered_indices"] == [0, 1, 2]
    assert proof["boundary_index"] == 3
    assert proof["boundary_confirmations"] == 2


def test_transport_failure_never_proves_boundary() -> None:
    proof = prove_contiguous_index_domain(
        [
            {"index": 0, "outcome": ACCEPTED},
            {"index": 1, "outcome": ACCEPTED},
            {"index": 2, "outcome": TRANSPORT_ERROR},
        ]
    )

    assert proof["state"] == UNRESOLVED
    assert proof["required_indices"] == []
    assert proof["covered_indices"] == [0, 1]
    assert proof["boundary_index"] is None


def test_contradictory_repeated_index_remains_unresolved() -> None:
    proof = prove_contiguous_index_domain(
        [
            {"index": 0, "outcome": ACCEPTED},
            {"index": 1, "outcome": ACCEPTED},
            {"index": 2, "outcome": SEMANTIC_REJECTION},
            {"index": 2, "outcome": ACCEPTED},
        ]
    )

    assert proof["state"] == UNRESOLVED


def test_rejection_at_zero_does_not_invent_an_empty_or_one_based_domain() -> None:
    proof = prove_contiguous_index_domain(
        [
            {"index": 0, "outcome": SEMANTIC_REJECTION},
            {"index": 0, "outcome": SEMANTIC_REJECTION},
        ]
    )

    assert proof["state"] == UNRESOLVED
    assert proof["boundary_index"] is None


def test_probe_stops_at_first_rejection_and_repeats_only_boundary() -> None:
    calls: list[int] = []

    def probe(index: int) -> dict:
        calls.append(index)
        return {
            "index": index,
            "outcome": ACCEPTED if index < 3 else SEMANTIC_REJECTION,
        }

    proof = probe_contiguous_index_domain(
        probe,
        max_index=16,
        boundary_confirmations=2,
    )

    assert proof["state"] == PROVEN
    assert proof["required_indices"] == [0, 1, 2]
    assert calls == [0, 1, 2, 3, 3]
