from __future__ import annotations

import re

from tester_spin.providers.rubyplay.execution import RubyPlayExecutionMixin


_ORIGINAL_TEST_GAME = RubyPlayExecutionMixin.test_game


def _is_unavailable_404(error: str) -> bool:
    text = str(error or "")
    return bool(
        re.search(r"\b404\b", text)
        and re.search(r"\b(?:not\s+found|client\s+error)\b", text, re.I)
    )


def test_game(self, game, *, spins, timeout_s, stop_event, progress):
    """Reclassify explicit RubyPlay HTTP 404 demos as unavailable, not broken.

    RubyPlay keeps some catalogue entries whose demo launcher/backend has been
    withdrawn. An HTTP 404 before any executable attempt is therefore an
    availability condition (SIN_DEMO), not a protocol/parser failure. Other HTTP
    statuses and any 404 after an actual gameserver attempt remain untouched.
    """
    result = _ORIGINAL_TEST_GAME(
        self,
        game,
        spins=spins,
        timeout_s=timeout_s,
        stop_event=stop_event,
        progress=progress,
    )
    if (
        result.status == "ERROR"
        and not result.attempts
        and _is_unavailable_404(result.error)
    ):
        original = str(result.error or "")
        result.status = "SIN_DEMO"
        result.requested_spins = 0
        result.successful_spins = 0
        result.failed_spins = 0
        result.error = (
            "RubyPlay: demo/launcher no disponible (HTTP 404); "
            "el juego está fuera de servicio."
            + (f" Diagnóstico original: {original}" if original else "")
        )
        progress(f"[{game.name}] SIN_DEMO: HTTP 404; juego fuera de servicio.")
    return result


def install_availability_classification() -> None:
    RubyPlayExecutionMixin.test_game = test_game
