from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from tester_spin.models import Game, GameTestResult
import tester_spin.providers.bgaming_exhaustive as bgaming_exhaustive


def test_raw_dynamic_index_probe_scopes_fresh_run_to_target_purchase() -> None:
    with tempfile.TemporaryDirectory() as temp:
        provider = bgaming_exhaustive.BGamingProvider(Path(temp) / "data")
        game = Game(
            provider="bgaming",
            slug="alice-wonderluck",
            name="Alice WonderLuck",
            url="https://example.invalid/alice",
        )
        result = GameTestResult(
            provider="bgaming",
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=1,
            successful_spins=0,
            failed_spins=1,
            status="PARCIAL",
            run_dir=str(Path(temp) / "probe-run"),
        )
        probe = {
            "target_reached": True,
            "outcome": "ACCEPTED",
            "value": 7,
        }

        with (
            patch.object(bgaming_exhaustive, "begin_flow_choice_run") as begin,
            patch.object(bgaming_exhaustive, "flow_choice_probe_result", return_value=probe),
            patch.object(bgaming_exhaustive, "end_flow_choice_run", return_value=[{"trace": 1}]) as end,
            patch.object(bgaming_exhaustive._execution, "set_execution_mode_filter") as set_filter,
            patch.object(bgaming_exhaustive._execution, "clear_execution_mode_filter") as clear_filter,
            patch.object(
                bgaming_exhaustive._BGamingProvider,
                "test_game",
                return_value=result,
            ) as run,
        ):
            sub, trace, observed = provider._raw_dynamic_index_probe(
                game,
                scope="PURCHASE_FREESPIN_BUY_LEVEL_0",
                command="pick_cards",
                prefix=('mode="select_pick_cards"',),
                variant={
                    "literal_options": {"mode": "any"},
                    "unresolved_fields": ["index"],
                    "source": "client",
                },
                index=7,
                timeout_s=5.0,
                stop_event=threading.Event(),
                progress=lambda _message: None,
            )

    assert sub is result
    assert trace == [{"trace": 1}]
    assert observed == probe
    set_filter.assert_called_once_with("PURCHASE_FREESPIN_BUY_LEVEL_0")
    clear_filter.assert_called_once()
    begin.assert_called_once_with(
        forced_scope="PURCHASE_FREESPIN_BUY_LEVEL_0",
        forced_command="pick_cards",
        forced_path=('mode="select_pick_cards"',),
        dynamic_probe={
            "scope": "PURCHASE_FREESPIN_BUY_LEVEL_0",
            "command": "pick_cards",
            "prefix": ['mode="select_pick_cards"'],
            "literal_options": {"mode": "any"},
            "field": "index",
            "value": 7,
        },
    )
    run.assert_called_once()
    end.assert_called_once()
