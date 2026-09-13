from __future__ import annotations

from typing import Any

from tester_spin.providers.bgaming import flow_choices
from tester_spin.providers.bgaming.server_guided import (
    analyze_server_response_against_bundle,
    begin_dynamic_contract_run,
    end_dynamic_contract_run,
    remember_dynamic_evidence,
)


def _response() -> dict[str, Any]:
    return {
        "flow": {
            "round_id": 77,
            "state": "gamble_bonus",
            "command": "spin",
            "available_actions": ["init", "gamble_bonus", "pick_cards"],
            "purchased_feature": {"name": "future_feature", "level": "0"},
        },
        "features": {
            "freespins_issued": 8,
            "freespins_left": 8,
            "cards_data": {"issued": 3, "list": [], "new": []},
        },
    }


def _bundle() -> str:
    return (
        'requestGamble=async()=>q.request({command:"gamble_bonus",options:{}});'
        'requestCardsPick=async e=>q.request({command:"pick_cards",options:e});'
        'onStart=async()=>requestCardsPick({mode:"select_pick_cards"});'
        'onAuto=async()=>requestCardsPick({mode:"auto"});'
        'onPlayer=async e=>requestCardsPick({mode:"any",index:e});'
    )


def test_dynamic_literal_variants_advance_instead_of_repeating(monkeypatch) -> None:
    data = _response()
    evidence = analyze_server_response_against_bundle(data, _bundle())
    sent: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(flow_choices, "_ORIGINAL_FLOW_CONTINUATION", lambda _data: "")
    monkeypatch.setattr(
        flow_choices,
        "_ORIGINAL_PENDING_FLOW_ACTIONS",
        lambda _data: ["gamble_bonus", "pick_cards"],
    )

    def fake_post(runtime, command: str, *, timeout_s: float, options=None, extra_data=None):
        payload = dict(options or {})
        sent.append((command, payload))
        return object(), {"command": command, "options": payload}, data

    monkeypatch.setattr(flow_choices, "_ORIGINAL_POST_COMMAND", fake_post)

    begin_dynamic_contract_run()
    remember_dynamic_evidence(evidence)
    flow_choices.begin_flow_choice_run()
    try:
        command = flow_choices._flow_continuation_with_choices(data)
        assert command == "gamble_bonus"
        flow_choices._post_command_with_choices(object(), command, timeout_s=3.0)
        assert sent[-1] == ("gamble_bonus", {})

        command = flow_choices._flow_continuation_with_choices(data)
        assert command == "pick_cards"
        flow_choices._post_command_with_choices(object(), command, timeout_s=3.0)
        assert sent[-1] == ("pick_cards", {"mode": "select_pick_cards"})

        command = flow_choices._flow_continuation_with_choices(data)
        assert command == "pick_cards"
        flow_choices._post_command_with_choices(object(), command, timeout_s=3.0)
        assert sent[-1] == ("pick_cards", {"mode": "auto"})

        # Both fully client-proven literal variants were consumed. The adapter
        # must stop rather than silently re-send select_pick_cards forever.
        assert flow_choices._flow_continuation_with_choices(data) == ""
        assert sent == [
            ("gamble_bonus", {}),
            ("pick_cards", {"mode": "select_pick_cards"}),
            ("pick_cards", {"mode": "auto"}),
        ]
    finally:
        flow_choices.end_flow_choice_run()
        end_dynamic_contract_run()


def test_forced_replay_can_choose_auto_at_root(monkeypatch) -> None:
    data = _response()
    evidence = analyze_server_response_against_bundle(data, _bundle())
    sent: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(flow_choices, "_ORIGINAL_FLOW_CONTINUATION", lambda _data: "")

    def fake_post(runtime, command: str, *, timeout_s: float, options=None, extra_data=None):
        payload = dict(options or {})
        sent.append((command, payload))
        return object(), {"command": command, "options": payload}, data

    monkeypatch.setattr(flow_choices, "_ORIGINAL_POST_COMMAND", fake_post)

    begin_dynamic_contract_run()
    remember_dynamic_evidence(evidence)
    flow_choices.begin_flow_choice_run(
        forced_scope="PURCHASE_FUTURE_FEATURE_LEVEL_0",
        forced_command="pick_cards",
        forced_path=('mode="auto"',),
    )
    try:
        command = flow_choices._flow_continuation_with_choices(data)
        assert command == "pick_cards"
        flow_choices._post_command_with_choices(object(), command, timeout_s=3.0)
        assert sent == [("pick_cards", {"mode": "auto"})]
    finally:
        flow_choices.end_flow_choice_run()
        end_dynamic_contract_run()
