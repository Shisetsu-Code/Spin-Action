from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tester_spin.feature_sessions import FEATURE_COMPLETE, FEATURE_INCOMPLETE
from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers import bgaming_exhaustive
from tester_spin.providers.bgaming import flow_choices
from tester_spin.providers.bgaming.feature_sessions import build_bgaming_feature_sessions
from tester_spin.providers.bgaming.server_guided import (
    analyze_server_response_against_bundle,
    begin_dynamic_contract_run,
    end_dynamic_contract_run,
    remember_dynamic_evidence,
)


def _server_state() -> dict:
    return {
        "flow": {
            "round_id": "alice-round",
            "state": "gamble_bonus",
            "available_actions": ["init", "gamble_bonus", "pick_cards"],
            "purchased_feature": {"name": "freespin_buy", "level": "0"},
        },
        "features": {
            "cards_data": {"issued": 16, "list": [], "new": []},
        },
    }


def _client_bundle() -> str:
    return (
        'requestGamble=async()=>q.request({command:"gamble_bonus",options:{}});'
        'requestCardsPick=async e=>q.request({command:"pick_cards",options:e});'
        'onStart=async()=>requestCardsPick({mode:"select_pick_cards"});'
        'onAuto=async()=>requestCardsPick({mode:"auto"});'
        'onPlayer=async e=>requestCardsPick({mode:"any",index:e});'
    )


def _step(root: Path, number: int, command: str, state: str) -> None:
    (root / f"step-{number:03d}-request.json").write_text(
        json.dumps({"command": command, "options": {}}),
        encoding="utf-8",
    )
    (root / f"step-{number:03d}-response.json").write_text(
        json.dumps({"flow": {"state": state, "round_id": "alice-round"}}),
        encoding="utf-8",
    )
    (root / f"step-{number:03d}-proof.json").write_text(
        json.dumps(
            {
                "round_id": "alice-round",
                "last_action_id": number,
                "win": 0,
            }
        ),
        encoding="utf-8",
    )


def test_alice_long_feature_counts_rounds_but_manual_picker_keeps_coverage_open() -> None:
    data = _server_state()
    evidence = analyze_server_response_against_bundle(data, _client_bundle())

    begin_dynamic_contract_run()
    remember_dynamic_evidence(evidence)
    flow_choices.begin_flow_choice_run()
    try:
        prompts = flow_choices._candidate_prompts(data)
        graph = {}
        for prompt in prompts:
            if prompt.command == "gamble_bonus":
                row = prompt.to_dict()
                row["selected"] = "__execute__"
                row["path_after"] = ["__execute__"]
                bgaming_exhaustive._merge_choice_trace(graph, [row], complete=True)
            elif prompt.command == "pick_cards":
                for selected in ('mode="select_pick_cards"', 'mode="auto"'):
                    row = prompt.to_dict()
                    row["selected"] = selected
                    row["path_after"] = [selected]
                    bgaming_exhaustive._merge_choice_trace(graph, [row], complete=True)
    finally:
        flow_choices.end_flow_choice_run()
        end_dynamic_contract_run()

    modes = [
        bgaming_exhaustive._choice_mode_from_point(point, repetitions=1)
        for point in graph.values()
    ]
    pick_mode = next(mode for mode in modes if mode["wire_command"] == "pick_cards")
    assert "DOMAIN_UNRESOLVED" in pick_mode["required_options"]
    assert pick_mode["unresolved_option_variants"][0]["literal_options"] == {"mode": "any"}
    assert pick_mode["unresolved_option_variants"][0]["unresolved_fields"] == ["index"]

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        attempt_dir = root / "PURCHASE_FREESPIN_BUY_LEVEL_0" / "attempt-001"
        attempt_dir.mkdir(parents=True)

        _step(attempt_dir, 1, "spin", "gamble_bonus")
        _step(attempt_dir, 2, "gamble_bonus", "pick_cards")
        _step(attempt_dir, 3, "pick_cards", "freespins")
        for number in range(4, 14):
            _step(attempt_dir, number, "freespin", "freespins")
        _step(attempt_dir, 14, "respin", "freespins")
        _step(attempt_dir, 15, "freespin", "closed")

        attempt = SpinAttempt(
            number=1,
            ok=True,
            mode_id="PURCHASE_FREESPIN_BUY_LEVEL_0",
            mode_kind="PURCHASE",
            terminal=True,
            wire_steps=15,
            artifact_dir=str(attempt_dir),
        )
        result = GameTestResult(
            provider="bgaming",
            slug="alice-wonderluck",
            game_name="Alice WonderLuck",
            game_url="https://bgaming.com/games/alice-wonderluck",
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
            run_dir=str(root),
            attempts=[attempt],
            discovered_modes=modes,
        )

        report = build_bgaming_feature_sessions(result)

    assert report["session_count"] == 1
    session = report["sessions"][0]
    assert session["totals"]["logical_rounds"] == 12
    assert session["totals"]["wire_steps"] == 15
    assert session["state"] == FEATURE_INCOMPLETE
    pick_choice = next(choice for choice in session["choices"] if choice["command"] == "pick_cards")
    assert "DOMAIN_UNRESOLVED" in pick_choice["missing_options"]


def test_alice_proven_sixteen_card_domain_can_close_feature_session() -> None:
    variant = {
        "literal_options": {"mode": "any"},
        "unresolved_fields": ["index"],
        "source": "client-callsite:requestCardsPick",
    }
    pick_point = {
        "scope": "PURCHASE_FREESPIN_BUY_LEVEL_0",
        "command": "pick_cards",
        "option_field": "<options>",
        "source": "server-guided-client",
        "prefix": ('mode="select_pick_cards"',),
        "available": ('mode="auto"',),
        "covered": {'mode="auto"'},
        "sample_counts": {'mode="auto"': 1},
        "unresolved_option_variants": [variant],
    }
    graph = {
        (
            "PURCHASE_FREESPIN_BUY_LEVEL_0",
            "pick_cards",
            ('mode="select_pick_cards"',),
        ): pick_point,
    }

    changed, proofs = bgaming_exhaustive._resolve_dynamic_index_domains(
        graph,
        probe_value=lambda _point, _variant, index: {
            "index": index,
            "outcome": "ACCEPTED" if index < 16 else "SEMANTIC_REJECTION",
        },
        max_index=32,
        boundary_confirmations=2,
    )
    assert changed is True
    proof = next(row for row in proofs if row["state"] == "PROVEN")
    assert proof["boundary_index"] == 16
    assert proof["required_indices"] == list(range(16))

    dynamic_labels = list(pick_point["dynamic_option_payloads"])
    assert len(dynamic_labels) == 16
    for label in dynamic_labels:
        pick_point["covered"].add(label)
        pick_point["sample_counts"][label] = 1

    pick_mode = bgaming_exhaustive._choice_mode_from_point(
        pick_point,
        repetitions=1,
    )
    assert "DOMAIN_UNRESOLVED" not in pick_mode["required_options"]
    assert set(pick_mode["required_options"]) == set(pick_mode["covered_options"])

    gamble_mode = {
        "id": "BGAMING_FLOW_CHOICE_PURCHASE_FREESPIN_BUY_LEVEL_0__GAMBLE_BONUS__ROOT",
        "kind": "CHOICE_CONTINUATION",
        "scope": "PURCHASE_FREESPIN_BUY_LEVEL_0",
        "parent": "PURCHASE_FREESPIN_BUY_LEVEL_0",
        "wire_command": "gamble_bonus",
        "branch_signature": (
            "BGAMING:flow-choice:PURCHASE_FREESPIN_BUY_LEVEL_0:"
            "gamble_bonus:[]"
        ),
        "path_prefix": [],
        "required_options": ["__execute__"],
        "covered_options": ["__execute__"],
        "required_samples": 1,
        "sample_counts": {"__execute__": 1},
    }

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        attempt_dir = root / "PURCHASE_FREESPIN_BUY_LEVEL_0" / "attempt-001"
        attempt_dir.mkdir(parents=True)

        _step(attempt_dir, 1, "spin", "gamble_bonus")
        _step(attempt_dir, 2, "gamble_bonus", "pick_cards")
        _step(attempt_dir, 3, "pick_cards", "freespins")
        for number in range(4, 14):
            _step(attempt_dir, number, "freespin", "freespins")
        _step(attempt_dir, 14, "respin", "freespins")
        _step(attempt_dir, 15, "freespin", "closed")

        attempt = SpinAttempt(
            number=1,
            ok=True,
            mode_id="PURCHASE_FREESPIN_BUY_LEVEL_0",
            mode_kind="PURCHASE",
            terminal=True,
            wire_steps=15,
            artifact_dir=str(attempt_dir),
        )
        result = GameTestResult(
            provider="bgaming",
            slug="alice-wonderluck",
            game_name="Alice WonderLuck",
            game_url="https://bgaming.com/games/alice-wonderluck",
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
            run_dir=str(root),
            attempts=[attempt],
            discovered_modes=[gamble_mode, pick_mode],
        )

        report = build_bgaming_feature_sessions(result)

    session = report["sessions"][0]
    assert session["totals"]["logical_rounds"] == 12
    assert session["state"] == FEATURE_COMPLETE
    assert all(choice["complete"] for choice in session["choices"])
