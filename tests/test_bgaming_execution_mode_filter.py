from __future__ import annotations

from tester_spin.providers.bgaming import execution


def _modes() -> list[dict]:
    return [
        {"id": "SPIN", "kind": "SPIN"},
        {"id": "PURCHASE_A", "kind": "PURCHASE"},
        {"id": "PURCHASE_B", "kind": "PURCHASE"},
    ]


def test_execution_mode_filter_is_inert_by_default() -> None:
    execution.clear_execution_mode_filter()
    assert execution.filter_execution_mode_specs(_modes()) == _modes()


def test_execution_mode_filter_keeps_only_requested_purchase() -> None:
    execution.set_execution_mode_filter("PURCHASE_B")
    try:
        assert execution.filter_execution_mode_specs(_modes()) == [
            {"id": "PURCHASE_B", "kind": "PURCHASE"}
        ]
    finally:
        execution.clear_execution_mode_filter()


def test_execution_mode_filter_fails_closed_when_target_is_absent() -> None:
    execution.set_execution_mode_filter("PURCHASE_MISSING")
    try:
        try:
            execution.filter_execution_mode_specs(_modes())
        except ValueError as exc:
            assert "PURCHASE_MISSING" in str(exc)
        else:
            raise AssertionError("missing mode filter must fail closed")
    finally:
        execution.clear_execution_mode_filter()
