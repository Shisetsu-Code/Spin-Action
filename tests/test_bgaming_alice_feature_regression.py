from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tester_spin.feature_sessions import FEATURE_COMPLETE, FEATURE_INCOMPLETE
from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers import bgaming_exhaustive
from tester_spin.providers.bgaming.feature_sessions import build_bgaming_feature_sessions


_SEQUENCE = 'mode="any"|index=<server-sequence>'
_SELECT = 'mode="select_pick_cards"'
_AUTO = 'mode="auto"'


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


def _choice_modes(*, manual_covered: bool) -> list[dict]:
    scope = "PURCHASE_FREESPIN_BUY_LEVEL_0"
    root_point = {
        "scope": scope,
        "command": "pick_cards",
        "option_field": "<options>",
        "source": "server-guided-client",
        "prefix": (),
        "available": (_SELECT, _AUTO),
        "covered": {_SELECT, _AUTO},
        "sample_counts": {_SELECT: 1, _AUTO: 1},
        "unresolved_option_variants": [],
    }
    child_covered = {_AUTO}
    child_counts = {_AUTO: 1, _SEQUENCE: 0}
    if manual_covered:
        child_covered.add(_SEQUENCE)
        child_counts[_SEQUENCE] = 1
    child_point = {
        "scope": scope,
        "command": "pick_cards",
        "option_field": "<options>",
        "source": "server-guided-client",
        "prefix": (_SELECT,),
        "available": (_AUTO, _SEQUENCE),
        "covered": child_covered,
        "sample_counts": child_counts,
        "unresolved_option_variants": [],
        "server_sequence_contracts": {
            _SEQUENCE: {
                "authority": "features.cards_data.issued+list",
                "field": "index",
            }
        },
    }
    return [
        bgaming_exhaustive._choice_mode_from_point(root_point, repetitions=1),
        bgaming_exhaustive._choice_mode_from_point(child_point, repetitions=1),
    ]


def _gamble_mode() -> dict:
    return {
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


def _feature_report(*, manual_covered: bool) -> dict:
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
            discovered_modes=[
                _gamble_mode(),
                *_choice_modes(manual_covered=manual_covered),
            ],
        )
        return build_bgaming_feature_sessions(result)


def test_alice_manual_picker_is_one_server_sequence_not_sixteen_index_branches() -> None:
    modes = _choice_modes(manual_covered=False)
    child = next(
        mode for mode in modes
        if mode["path_prefix"] == [_SELECT]
    )

    assert child["required_options"] == [_AUTO, _SEQUENCE]
    assert child["covered_options"] == [_AUTO]
    assert "DOMAIN_UNRESOLVED" not in child["required_options"]
    assert not any(
        value.startswith("index=") and "<server-sequence>" not in value
        for value in child["required_options"]
    )


def test_alice_long_feature_stays_incomplete_until_manual_sequence_is_covered() -> None:
    report = _feature_report(manual_covered=False)

    assert report["session_count"] == 1
    session = report["sessions"][0]
    assert session["totals"]["logical_rounds"] == 12
    assert session["totals"]["wire_steps"] == 15
    assert session["state"] == FEATURE_INCOMPLETE

    child = next(
        choice for choice in session["choices"]
        if choice["command"] == "pick_cards"
        and choice["prefix"] == [_SELECT]
    )
    assert child["missing_options"] == [_SEQUENCE]
    assert "DOMAIN_UNRESOLVED" not in child["missing_options"]


def test_alice_server_issued_manual_sequence_can_close_feature_session() -> None:
    report = _feature_report(manual_covered=True)

    session = report["sessions"][0]
    assert session["totals"]["logical_rounds"] == 12
    assert session["state"] == FEATURE_COMPLETE
    assert all(choice["complete"] for choice in session["choices"])
