from __future__ import annotations

import threading

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.redtiger import execution as redtiger_execution
from tester_spin.providers.redtiger.adapter import RedTigerProvider as RedTigerAdapter
from tester_spin.providers.redtiger.evolution_launch import bootstrap_game as evolution_bootstrap_game


_METADATA_ONLY_GAP = "GAME_MODES_WIRE_CONTRACT"


def _normalize_metadata_only_partial(result: GameTestResult) -> None:
    """Keep settings-only gameModes as diagnostics, not failed executable coverage.

    Red Tiger exposes ``gameModes`` in settings for many titles, while the actual
    executable actions are the normal spin and the feature-buy contracts. The
    resulting runtime transitions (Normal, Respin, FreeSpins, etc.) are observed
    inside successful spin responses. If every requested executable action passed
    and the only reason for PARCIAL is the legacy GAME_MODES_WIRE_CONTRACT marker,
    the run is complete and should be reported as OK.
    """
    if result.status != "PARCIAL":
        return
    if result.requested_spins <= 0 or result.successful_spins != result.requested_spins:
        return
    if result.failed_spins != 0 or not result.attempts or any(not attempt.ok for attempt in result.attempts):
        return

    message = str(result.error or "")
    if _METADATA_ONLY_GAP not in message:
        return

    remainder = message.replace(
        f"Cobertura pendiente: {_METADATA_ONLY_GAP}.",
        "",
    ).strip()
    if "Cobertura pendiente:" in remainder or "Errores:" in remainder or "Diagnóstico:" in remainder:
        return

    result.status = "OK"
    result.error = ""


class RedTigerProvider(RedTigerAdapter):
    """Public provider boundary preserving catalog launch and runtime identities."""

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        launch_id = self.launch_id_for_game(game)
        original_bootstrap = redtiger_execution.bootstrap_game
        redtiger_execution.bootstrap_game = evolution_bootstrap_game
        try:
            result = super().test_game(
                game,
                spins=spins,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
            )
        finally:
            redtiger_execution.bootstrap_game = original_bootstrap

        _normalize_metadata_only_partial(result)
        if launch_id:
            game.symbol = launch_id
            result.symbol = launch_id
        return result
