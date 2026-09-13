from __future__ import annotations

from typing import Any

from tester_spin.providers.bgaming.contracts import (
    choice_costs,
    choice_values,
    command_contract,
)
from tester_spin.providers.bgaming import flow_choices


def _response(
    *,
    prices: dict[str, int | float],
    wallet: int | float = 100,
) -> dict[str, Any]:
    return {
        "flow": {
            "round_id": 77,
            "state": "freespins",
            "command": "spin",
            "purchased_feature": {"name": "future_feature"},
            "available_actions": ["buy_extra_bonus", "freespin", "init"],
        },
        "features": {
            "bonus_data": {
                "bonus_game_prices": dict(prices),
            }
        },
        "balance": {"wallet": wallet, "game": 0},
    }


def test_buy_extra_bonus_contract_is_provider_level_and_runtime_driven() -> None:
    contract = command_contract("buy_extra_bonus")

    assert contract is not None
    assert contract.choice is not None
    assert contract.choice.option_field == "bonus_type"
    assert contract.choice.container_path == (
        "features",
        "bonus_data",
        "bonus_game_prices",
    )
    assert contract.choice.mapping_keys is True
    assert contract.choice.mapping_values_are_debit is True

    # Deliberately use option names that do not exist in Hottest666. The
    # contract must consume whatever finite domain the runtime advertises.
    data = _response(prices={"future_alpha": 10, "future_beta": 25})
    assert choice_values(data, "buy_extra_bonus") == [
        "future_alpha",
        "future_beta",
    ]
    assert choice_costs(data, "buy_extra_bonus") == {
        "future_alpha": 10.0,
        "future_beta": 25.0,
    }


def test_buy_extra_bonus_filters_choices_disabled_by_client_balance() -> None:
    data = _response(
        prices={
            "affordable": 40,
            "too_expensive": 120,
            "disabled_zero": 0,
        },
        wallet=100,
    )

    assert choice_values(data, "buy_extra_bonus") == ["affordable"]
    assert choice_costs(data, "buy_extra_bonus") == {"affordable": 40.0}


def test_simultaneous_freespin_keeps_normal_path_but_records_choice(monkeypatch) -> None:
    data = _response(prices={"alpha": 10, "beta": 20})
    monkeypatch.setattr(
        flow_choices,
        "_ORIGINAL_FLOW_CONTINUATION",
        lambda _data: "freespin",
    )
    monkeypatch.setattr(
        flow_choices,
        "_ORIGINAL_PENDING_FLOW_ACTIONS",
        lambda _data: ["buy_extra_bonus"],
    )

    flow_choices.begin_flow_choice_run()
    try:
        assert flow_choices._flow_continuation_with_choices(data) == "freespin"
        pending = flow_choices._pending_flow_actions_with_choices(data)
        assert "buy_extra_bonus" not in pending
    finally:
        trace = flow_choices.end_flow_choice_run()

    assert len(trace) == 1
    assert trace[0]["command"] == "buy_extra_bonus"
    assert trace[0]["option_field"] == "bonus_type"
    assert trace[0]["available"] == ["alpha", "beta"]
    assert trace[0]["selected"] == ""


def test_forced_extra_bonus_overrides_freespin_and_validates_advertised_debit(
    monkeypatch,
) -> None:
    data = _response(prices={"alpha": 15, "beta": 35})
    sent: dict[str, Any] = {}
    validated: dict[str, Any] = {}

    monkeypatch.setattr(
        flow_choices,
        "_ORIGINAL_FLOW_CONTINUATION",
        lambda _data: "freespin",
    )

    def fake_post(
        runtime,
        command: str,
        *,
        timeout_s: float,
        options=None,
        extra_data=None,
    ):
        sent.update(
            {
                "runtime": runtime,
                "command": command,
                "timeout_s": timeout_s,
                "options": dict(options or {}),
                "extra_data": extra_data,
            }
        )
        return object(), {"command": command, "options": dict(options or {})}, {}

    def fake_validate(payload: dict[str, Any], **kwargs):
        validated.update(kwargs)
        return []

    monkeypatch.setattr(flow_choices, "_ORIGINAL_POST_COMMAND", fake_post)
    monkeypatch.setattr(flow_choices, "_ORIGINAL_VALIDATE_SPIN", fake_validate)

    flow_choices.begin_flow_choice_run(
        forced_scope="PURCHASE_FUTURE_FEATURE",
        forced_command="buy_extra_bonus",
        forced_path=("beta",),
    )
    try:
        command = flow_choices._flow_continuation_with_choices(data)
        assert command == "buy_extra_bonus"

        flow_choices._post_command_with_choices(
            object(),
            command,
            timeout_s=3.0,
        )
        assert sent["command"] == "buy_extra_bonus"
        assert sent["options"] == {"bonus_type": "beta"}

        assert flow_choices._validate_spin_with_choices(
            {},
            requested_bet=1,
            previous_balance_total=100,
            expected_reels=5,
            expected_rows=3,
            command="buy_extra_bonus",
            expected_debit=0,
        ) == []
    finally:
        trace = flow_choices.end_flow_choice_run()

    assert validated["expected_debit"] == 35.0
    assert validated["allow_observed_debit"] is False
    assert trace[-1]["selected"] == "beta"
    assert trace[-1]["expected_debit"] == 35.0
