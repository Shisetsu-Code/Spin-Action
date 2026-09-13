from __future__ import annotations

from unittest.mock import patch

from tester_spin.providers.bgaming import execution
from tester_spin.providers.bgaming import flow_choices
from tester_spin.providers.bgaming import runtime
from tester_spin.providers.bgaming.contracts import (
    CONTINUATION_BY_STATE,
    SAFE_CONTINUATION_COMMANDS,
    command_contract,
)


class _Response:
    status_code = 200


class _Runtime:
    pass


def _choice_payload(
    names: list[str],
    *,
    round_id: int = 777,
    purchased: str = "freespin_buy",
    level: str = "4",
    action: str = "select_bonus",
):
    return {
        "api_version": "2",
        "outcome": {
            "bet": 200,
            "win": 0,
            "screen": [[1], [2], [3], [4], [5]],
        },
        "balance": {"wallet": 1000, "game": 0},
        "flow": {
            "round_id": round_id,
            "last_action_id": f"{round_id}_1",
            "state": "select_bonus",
            "command": "freespin",
            "available_actions": ["init", action],
            "purchased_feature": {"name": purchased, "level": level},
        },
        "game": {
            "freespin_params": {
                "variants": [
                    {"name": name, "spins": index + 5}
                    for index, name in enumerate(names)
                ]
            }
        },
    }


def test_contract_registry_derives_legacy_views_without_choice_hardcoding():
    contract = command_contract("select_bonus")
    assert contract is not None
    assert contract.choice is not None
    assert contract.choice.option_field == "name"
    assert "select_bonus" not in SAFE_CONTINUATION_COMMANDS
    assert "select_bonus" not in CONTINUATION_BY_STATE
    assert CONTINUATION_BY_STATE["freespins"] == "freespin"


def test_state_name_is_never_promoted_to_a_different_server_action():
    flow_choices.install_flow_choice_adapter()
    payload = _choice_payload(
        ["reward_a", "reward_b"],
        action="play_bonus_game",
    )

    # Adventures exposed exactly this shape: state=select_bonus but the server
    # action was play_bonus_game.  The state is descriptive, never a command.
    assert execution.flow_continuation_command(payload) == ""
    assert execution.pending_flow_actions(payload) == ["play_bonus_game"]
    assert flow_choices.flow_choice_options(payload, "select_bonus") == []


def test_choice_action_requires_finite_runtime_domain():
    flow_choices.install_flow_choice_adapter()
    payload = _choice_payload(["reward_a", "reward_b"])
    assert flow_choices.flow_choice_options(payload, "select_bonus") == [
        "reward_a",
        "reward_b",
    ]
    assert flow_choices.flow_choice_scope(payload) == "PURCHASE_FREESPIN_BUY_LEVEL_4"

    missing_domain = {**payload, "game": {}}
    assert execution.flow_continuation_command(missing_domain) == ""
    assert execution.pending_flow_actions(missing_domain) == ["select_bonus"]


def test_choice_adapter_sends_contract_field_and_records_nested_path():
    flow_choices.install_flow_choice_adapter()
    sent: list[dict] = []

    def fake_post(runtime_obj, command, *, timeout_s, options=None, extra_data=None):
        sent.append({
            "command": command,
            "options": dict(options or {}),
        })
        return _Response(), {"command": command, "options": dict(options or {})}, {
            "api_version": "2",
            "outcome": {"bet": 200, "win": 0, "screen": [[1]]},
            "balance": {"wallet": 1000, "game": 0},
            "flow": {
                "round_id": 777,
                "last_action_id": "777_x",
                "state": "freespins",
                "command": command,
                "available_actions": ["init", "freespin"],
                "purchased_feature": {"name": "freespin_buy", "level": "4"},
            },
        }

    flow_choices.begin_flow_choice_run(
        forced_scope="PURCHASE_FREESPIN_BUY_LEVEL_4",
        forced_path=("reward_b", "reward_d"),
    )
    try:
        with patch.object(flow_choices, "_ORIGINAL_POST_COMMAND", fake_post):
            first = _choice_payload(["reward_a", "reward_b"])
            assert execution.pending_flow_actions(first) == []
            assert execution.flow_continuation_command(first) == "select_bonus"
            execution.post_command(_Runtime(), "select_bonus", timeout_s=1.0)

            second = _choice_payload(["reward_c", "reward_d"])
            assert execution.flow_continuation_command(second) == "select_bonus"
            execution.post_command(_Runtime(), "select_bonus", timeout_s=1.0)
    finally:
        trace = flow_choices.end_flow_choice_run()

    assert [item["options"] for item in sent] == [
        {"name": "reward_b"},
        {"name": "reward_d"},
    ]
    assert trace[0]["command"] == "select_bonus"
    assert trace[0]["prefix"] == []
    assert trace[0]["selected"] == "reward_b"
    assert trace[0]["path_after"] == ["reward_b"]
    assert trace[1]["prefix"] == ["reward_b"]
    assert trace[1]["selected"] == "reward_d"
    assert trace[1]["path_after"] == ["reward_b", "reward_d"]


def test_validate_freespin_accepts_proven_choice_continuation():
    flow_choices.install_flow_choice_adapter()
    payload = _choice_payload(["reward_a", "reward_b"])

    flow_choices.begin_flow_choice_run()
    try:
        warnings = runtime.validate_spin(
            payload,
            requested_bet=200,
            previous_balance_total=1000,
            expected_reels=5,
            expected_rows=1,
            command="freespin",
            expected_debit=0,
        )
    finally:
        flow_choices.end_flow_choice_run()

    assert warnings == []
