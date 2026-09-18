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


def test_proven_domain_materializes_replayable_index_options() -> None:
    from tester_spin.providers.bgaming.dynamic_index_domains import (
        apply_index_domain_proof,
        dynamic_index_option_label,
    )

    point = {
        "scope": "PURCHASE_FREESPIN_BUY_LEVEL_0",
        "command": "pick_cards",
        "prefix": ('mode="select_pick_cards"',),
        "available": ('mode="auto"',),
        "covered": {"mode=\"auto\""},
        "sample_counts": {'mode="auto"': 1},
        "unresolved_option_variants": [
            {
                "literal_options": {"mode": "any"},
                "unresolved_fields": ["index"],
                "source": "client-callsite:requestCardsPick",
            }
        ],
    }
    variant = point["unresolved_option_variants"][0]
    proof = {
        "state": PROVEN,
        "required_indices": [0, 1, 2],
        "covered_indices": [0, 1, 2],
        "boundary_index": 3,
        "boundary_confirmations": 2,
        "probes": [],
    }

    changed = apply_index_domain_proof(point, variant, proof)

    assert changed is True
    assert point["unresolved_option_variants"] == []
    labels = [
        dynamic_index_option_label({"mode": "any"}, "index", index)
        for index in range(3)
    ]
    assert all(label in point["available"] for label in labels)
    assert all(label not in point["covered"] for label in labels)
    assert point["dynamic_option_payloads"][labels[1]] == {
        "mode": "any",
        "index": 1,
    }
    assert point["dynamic_index_proofs"][0]["boundary_index"] == 3


def test_unresolved_domain_does_not_materialize_options() -> None:
    from tester_spin.providers.bgaming.dynamic_index_domains import apply_index_domain_proof

    variant = {
        "literal_options": {"mode": "any"},
        "unresolved_fields": ["index"],
        "source": "client-callsite:requestCardsPick",
    }
    point = {
        "available": ('mode="auto"',),
        "covered": set(),
        "sample_counts": {},
        "unresolved_option_variants": [variant],
    }
    proof = {
        "state": UNRESOLVED,
        "required_indices": [],
        "covered_indices": [0, 1],
        "boundary_index": None,
        "boundary_confirmations": 0,
        "probes": [],
    }

    changed = apply_index_domain_proof(point, variant, proof)

    assert changed is False
    assert point["unresolved_option_variants"] == [variant]
    assert point.get("dynamic_option_payloads", {}) == {}
    assert point["dynamic_index_proofs"][0]["state"] == UNRESOLVED


def test_exhaustive_resolver_probes_and_registers_proven_domain() -> None:
    from tester_spin.providers import bgaming_exhaustive

    point = {
        "scope": "PURCHASE_FREESPIN_BUY_LEVEL_0",
        "command": "pick_cards",
        "prefix": ('mode="select_pick_cards"',),
        "available": ('mode="auto"',),
        "covered": {'mode="auto"'},
        "sample_counts": {'mode="auto"': 1},
        "unresolved_option_variants": [
            {
                "literal_options": {"mode": "any"},
                "unresolved_fields": ["index"],
                "source": "client-callsite:requestCardsPick",
            }
        ],
    }
    graph = {
        ("PURCHASE_FREESPIN_BUY_LEVEL_0", "pick_cards", ('mode="select_pick_cards"',)): point
    }
    calls: list[int] = []
    registered: list[tuple[str, dict]] = []

    def probe(_point: dict, _variant: dict, index: int) -> dict:
        calls.append(index)
        return {
            "index": index,
            "outcome": ACCEPTED if index < 3 else SEMANTIC_REJECTION,
        }

    changed, proofs = bgaming_exhaustive._resolve_dynamic_index_domains(
        graph,
        probe_value=probe,
        register_option=lambda _point, label, payload, _source: registered.append(
            (label, payload)
        ),
        max_index=8,
        boundary_confirmations=2,
    )

    assert changed is True
    assert calls == [0, 1, 2, 3, 3]
    assert len(proofs) == 1
    assert proofs[0]["state"] == PROVEN
    assert point["unresolved_option_variants"] == []
    assert len(registered) == 3
    assert registered[2][1] == {"mode": "any", "index": 2}


def test_exhaustive_resolver_ignores_non_index_dynamic_variants() -> None:
    from tester_spin.providers import bgaming_exhaustive

    point = {
        "scope": "PURCHASE_A",
        "command": "choose",
        "prefix": (),
        "available": ("auto",),
        "covered": set(),
        "sample_counts": {},
        "unresolved_option_variants": [
            {
                "literal_options": {"mode": "manual"},
                "unresolved_fields": ["row", "column"],
                "source": "client",
            }
        ],
    }
    graph = {("PURCHASE_A", "choose", ()): point}

    changed, proofs = bgaming_exhaustive._resolve_dynamic_index_domains(
        graph,
        probe_value=lambda *_args: (_ for _ in ()).throw(
            AssertionError("non-index variant must not be probed")
        ),
        max_index=8,
    )

    assert changed is False
    assert proofs == []
    assert point["unresolved_option_variants"]
