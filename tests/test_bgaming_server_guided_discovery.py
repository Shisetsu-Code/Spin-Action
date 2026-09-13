from __future__ import annotations

from tester_spin.providers.bgaming.server_guided import (
    analyze_server_response_against_bundle,
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

    tokens = {item["token"] for item in evidence["search_seeds"]}
    assert {
        "buy_extra_bonus",
        "bonus_game_prices",
        "extra_spin",
        "extra_board",
        "extra_multiplier",
        "regenerate_bonus",
    }.issubset(tokens)
