from __future__ import annotations

from typing import Any

from tester_spin.providers.bgaming import flow_choices
from tester_spin.providers.bgaming.server_guided import (
    analyze_server_response_against_bundle,
    begin_dynamic_contract_run,
    dynamic_action_unresolved_variants,
    dynamic_action_variants,
    end_dynamic_contract_run,
    remember_dynamic_evidence,
    server_search_seeds,
)


def _server_response() -> dict:
    return {
        "flow": {
            "state": "feature_round",
            "command": "spin",
            "available_actions": ["init", "freespin", "choose_future"],
        },
        "features": {
            "future_data": {
                "future_prices": {
                    "alpha_choice": 12,
                    "beta_choice": 24,
                }
            }
        },
    }


def _alice_shaped_response() -> dict[str, Any]:
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
            "cards_data": {
                "issued": 3,
                "list": [],
                "new": [],
            },
        },
    }


def _alice_shaped_bundle() -> str:
    return (
        'requestGamble=async()=>{return q.request({command:"gamble_bonus",options:{}})};'
        'requestCardsPick=async e=>{return q.request({command:"pick_cards",options:e})};'
        'onStart=async()=>this.requestCardsPick({mode:"select_pick_cards"});'
        'onAuto=async()=>this.requestCardsPick({mode:"auto"});'
        'onPlayer=async e=>this.requestCardsPick({mode:"any",index:e});'
    )


def test_server_response_produces_action_and_mapping_search_seeds() -> None:
    seeds = server_search_seeds(_server_response())
    by_token = {item["token"]: item for item in seeds}

    assert by_token["choose_future"]["kind"] == "available-action"
    assert by_token["future_prices"]["kind"] == "field"
    assert by_token["alpha_choice"]["kind"] == "mapping-key"
    assert by_token["beta_choice"]["kind"] == "mapping-key"


def test_server_action_locates_generic_client_serializer_without_title_rules() -> None:
    bundle = (
        "class Client{chooseFuture(t){"
        "const prices=this.features.future_data.future_prices;"
        "return this.request({command:'choose_future',options:{selection:t}})"
        "}}"
    )

    evidence = analyze_server_response_against_bundle(
        _server_response(),
        bundle,
        source="https://provider.invalid/version/bundle.js",
    )

    action = next(
        item for item in evidence["actions"]
        if item["action"] == "choose_future"
    )
    assert action["client_command_literal_hits"] == 1
    assert action["option_fields"] == ["selection"]
    assert action["serializer_shape_proven"] is True
    assert action["replay_eligible"] is False
    assert action["execution_authority"] == "evidence-only"

    seeds = {item["token"]: item for item in evidence["search_seeds"]}
    assert seeds["future_prices"]["client_hits"] == 1
    assert "choose_future" in seeds["future_prices"]["near_actions"]


def test_literal_without_options_remains_non_executable_evidence() -> None:
    bundle = "function x(){return request({command:'choose_future'})}"
    evidence = analyze_server_response_against_bundle(_server_response(), bundle)

    action = next(
        item for item in evidence["actions"]
        if item["action"] == "choose_future"
    )
    assert action["client_command_literal_hits"] == 1
    assert action["option_fields"] == []
    assert action["serializer_shape_proven"] is False
    assert action["replay_eligible"] is False
    assert action["execution_authority"] == "evidence-only"


def test_hottest_shaped_response_is_discovered_from_structure_not_game_name() -> None:
    data = {
        "flow": {
            "state": "freespins",
            "command": "spin",
            "available_actions": ["init", "freespin", "buy_extra_bonus"],
            "purchased_feature": {"name": "some_feature"},
        },
        "features": {
            "bonus_data": {
                "bonus_game_params": {
                    "spins_count": 4,
                    "boards_count": 2,
                    "multiplier": 1,
                },
                "bonus_game_prices": {
                    "extra_spin": 74,
                    "extra_board": 148,
                    "extra_multiplier": 295,
                    "regenerate_bonus": 1286,
                },
            }
        },
    }
    bundle = (
        "buyExtraBonus({bonus_type:t}){"
        "const p=this.features.bonus_data.bonus_game_prices;"
        "return request({command:\"buy_extra_bonus\",options:{bonus_type:t}})"
        "}"
    )

    evidence = analyze_server_response_against_bundle(data, bundle)
    action = next(
        item for item in evidence["actions"]
        if item["action"] == "buy_extra_bonus"
    )
    assert action["option_fields"] == ["bonus_type"]
    assert action["serializer_shape_proven"] is True
    # The command is executable through the static provider contract; this
    # generic scanner correctly refuses to invent t from a client variable.
    assert action["replay_eligible"] is False

    tokens = {item["token"] for item in evidence["search_seeds"]}
    assert {
        "buy_extra_bonus",
        "bonus_game_prices",
        "extra_spin",
        "extra_board",
        "extra_multiplier",
        "regenerate_bonus",
    }.issubset(tokens)


def test_forwarded_options_follow_client_callsites_without_game_hardcoding() -> None:
    evidence = analyze_server_response_against_bundle(
        _alice_shaped_response(),
        _alice_shaped_bundle(),
    )

    gamble = next(
        item for item in evidence["actions"]
        if item["action"] == "gamble_bonus"
    )
    assert gamble["parameterless"] is True
    assert gamble["serializer_shape_proven"] is True
    assert gamble["replay_eligible"] is True
    assert gamble["option_variants"] == [
        {
            "label": "__execute__",
            "options": {},
            "source": "client-inline-options",
        }
    ]

    pick = next(
        item for item in evidence["actions"]
        if item["action"] == "pick_cards"
    )
    assert pick["client_wrappers"] == ["requestCardsPick"]
    assert pick["forwarded_options_parameters"] == ["e"]
    assert pick["option_fields"] == ["mode", "index"]
    assert pick["replay_eligible"] is True
    assert {
        item["label"] for item in pick["option_variants"]
    } == {
        'mode="select_pick_cards"',
        'mode="auto"',
    }
    assert pick["unresolved_option_variants"] == [
        {
            "literal_options": {"mode": "any"},
            "unresolved_fields": ["index"],
            "source": "client-callsite:requestCardsPick",
        }
    ]


def test_dynamic_registry_promotes_only_fully_literal_client_payloads() -> None:
    data = _alice_shaped_response()
    evidence = analyze_server_response_against_bundle(data, _alice_shaped_bundle())

    begin_dynamic_contract_run()
    try:
        remember_dynamic_evidence(evidence)
        assert dynamic_action_variants(data, "gamble_bonus") == [
            {"label": "__execute__", "options": {}}
        ]
        assert dynamic_action_variants(data, "pick_cards") == [
            {
                "label": 'mode="select_pick_cards"',
                "options": {"mode": "select_pick_cards"},
            },
            {
                "label": 'mode="auto"',
                "options": {"mode": "auto"},
            },
        ]
        assert all(
            variant["options"].get("mode") != "any"
            for variant in dynamic_action_variants(data, "pick_cards")
        )
    finally:
        end_dynamic_contract_run()


def test_flow_choices_execute_parameterless_then_literal_forwarded_variant(monkeypatch) -> None:
    data = _alice_shaped_response()
    evidence = analyze_server_response_against_bundle(data, _alice_shaped_bundle())
    sent: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(flow_choices, "_ORIGINAL_FLOW_CONTINUATION", lambda _data: "")
    monkeypatch.setattr(
        flow_choices,
        "_ORIGINAL_PENDING_FLOW_ACTIONS",
        lambda _data: ["gamble_bonus", "pick_cards"],
    )

    def fake_post(runtime, command: str, *, timeout_s: float, options=None, extra_data=None):
        sent.append((command, dict(options or {})))
        return object(), {"command": command, "options": dict(options or {})}, data

    monkeypatch.setattr(flow_choices, "_ORIGINAL_POST_COMMAND", fake_post)

    begin_dynamic_contract_run()
    remember_dynamic_evidence(evidence)
    flow_choices.begin_flow_choice_run()
    try:
        pending = flow_choices._pending_flow_actions_with_choices(data)
        assert "gamble_bonus" not in pending
        assert "pick_cards" not in pending

        command = flow_choices._flow_continuation_with_choices(data)
        assert command == "gamble_bonus"
        flow_choices._post_command_with_choices(object(), command, timeout_s=3.0)
        assert sent[-1] == ("gamble_bonus", {})

        command = flow_choices._flow_continuation_with_choices(data)
        assert command == "pick_cards"
        flow_choices._post_command_with_choices(object(), command, timeout_s=3.0)
        assert sent[-1] == ("pick_cards", {"mode": "select_pick_cards"})
    finally:
        flow_choices.end_flow_choice_run()
        end_dynamic_contract_run()


def test_unresolved_dynamic_variants_are_scoped_to_observed_flow_state() -> None:
    data = _alice_shaped_response()
    evidence = analyze_server_response_against_bundle(data, _alice_shaped_bundle())

    begin_dynamic_contract_run()
    try:
        remember_dynamic_evidence(evidence)
        assert dynamic_action_unresolved_variants(data, "pick_cards")

        other = _alice_shaped_response()
        other["flow"]["state"] = "freespins"
        assert dynamic_action_unresolved_variants(other, "pick_cards") == []
    finally:
        end_dynamic_contract_run()
