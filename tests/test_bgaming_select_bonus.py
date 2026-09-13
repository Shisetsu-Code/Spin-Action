from __future__ import annotations

from unittest.mock import patch

from tester_spin.providers.bgaming import bonus_choice
from tester_spin.providers.bgaming import execution
from tester_spin.providers.bgaming import runtime


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
            "available_actions": ["init", "select_bonus"],
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


def test_select_bonus_requires_runtime_variant_domain():
    bonus_choice.install_bonus_choice_adapter()
    payload = _choice_payload(["reward_a", "reward_b"])

    assert bonus_choice.bonus_choice_options(payload) == ["reward_a", "reward_b"]
    assert bonus_choice.bonus_choice_scope(payload) == "PURCHASE_FREESPIN_BUY_LEVEL_4"

    missing_domain = {
        **payload,
        "game": {},
    }
    assert bonus_choice.bonus_choice_options(missing_domain) == []
    assert execution.flow_continuation_command(missing_domain) == ""
    assert "select_bonus" in execution.pending_flow_actions(missing_domain)


def test_select_bonus_sends_provider_observed_name_and_records_nested_path():
    bonus_choice.install_bonus_choice_adapter()
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

    bonus_choice.begin_bonus_choice_run(
        forced_scope="PURCHASE_FREESPIN_BUY_LEVEL_4",
        forced_path=("reward_b", "reward_d"),
    )
    try:
        with patch.object(bonus_choice, "_ORIGINAL_POST_COMMAND", fake_post):
            first = _choice_payload(["reward_a", "reward_b"])
            assert execution.pending_flow_actions(first) == []
            assert execution.flow_continuation_command(first) == "select_bonus"
            execution.post_command(_Runtime(), "select_bonus", timeout_s=1.0)

            second = _choice_payload(["reward_c", "reward_d"])
            assert execution.flow_continuation_command(second) == "select_bonus"
            execution.post_command(_Runtime(), "select_bonus", timeout_s=1.0)
    finally:
        trace = bonus_choice.end_bonus_choice_run()

    assert [item["options"] for item in sent] == [
        {"name": "reward_b"},
        {"name": "reward_d"},
    ]
    assert trace[0]["prefix"] == []
    assert trace[0]["selected"] == "reward_b"
    assert trace[0]["path_after"] == ["reward_b"]
    assert trace[1]["prefix"] == ["reward_b"]
    assert trace[1]["selected"] == "reward_d"
    assert trace[1]["path_after"] == ["reward_b", "reward_d"]


def test_validate_freespin_accepts_evidenced_select_bonus_state():
    bonus_choice.install_bonus_choice_adapter()
    payload = _choice_payload(["reward_a", "reward_b"])

    bonus_choice.begin_bonus_choice_run()
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
        bonus_choice.end_bonus_choice_run()

    assert warnings == []
