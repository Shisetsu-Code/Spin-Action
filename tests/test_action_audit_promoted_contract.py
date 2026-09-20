from __future__ import annotations

from tester_spin.action_audit import build_action_audit
from tester_spin.models import GameTestResult


def _result() -> GameTestResult:
    return GameTestResult(
        provider="bgaming",
        slug="alice-wonderluck",
        game_name="Alice WonderLuck",
        game_url="https://bgaming.com/games/alice-wonderluck",
        requested_spins=1,
        successful_spins=1,
        failed_spins=0,
        status="OK",
    )


def test_promoted_farm_contract_mode_ids_are_provider_proof() -> None:
    result = _result()
    result.structural_map = {
        "action_inventory": {
            "state": "COMPLETE",
            "source": "bgaming:promoted-farm-contract",
            "reason": (
                "promoted only after required modes are demonstrated and "
                "unresolved actions/continuations are empty"
            ),
            "mode_ids": ["PICK_CARDS", "FREESPIN"],
        }
    }
    result.discovered_modes = [
        {
            "id": "PICK_CARDS",
            "kind": "CONTINUATION",
            "observed": True,
            "executable": False,
        },
        {
            "id": "FREESPIN",
            "kind": "FEATURE",
            "observed": True,
            "executable": False,
        },
    ]

    audit = build_action_audit(result)

    assert audit["verdict"] == "COMPLETE"
    assert audit["counts"] == {
        "actions": 2,
        "demonstrated": 2,
        "incomplete": 0,
        "unknown": 0,
    }
    assert all(
        action["evidence"] == "validated promoted farm contract proof"
        for action in audit["actions"]
    )


def test_arbitrary_complete_inventory_does_not_replace_terminal_proof() -> None:
    result = _result()
    result.structural_map = {
        "action_inventory": {
            "state": "COMPLETE",
            "source": "provider-authoritative-init",
            "reason": "closed inventory",
            "mode_ids": ["PICK_CARDS"],
        }
    }
    result.discovered_modes = [
        {
            "id": "PICK_CARDS",
            "kind": "CONTINUATION",
            "observed": True,
            "executable": True,
        }
    ]

    audit = build_action_audit(result)

    assert audit["verdict"] == "INCOMPLETE"
    assert audit["actions"][0]["state"] == "INCOMPLETE"
