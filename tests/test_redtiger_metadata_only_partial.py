from __future__ import annotations

import unittest

from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers.redtiger.provider import _normalize_metadata_only_partial


class RedTigerMetadataOnlyPartialTests(unittest.TestCase):
    @staticmethod
    def _result(*, attempts: list[SpinAttempt], error: str, successful: int, requested: int) -> GameTestResult:
        return GameTestResult(
            provider="redtiger",
            slug="synthetic",
            game_name="Synthetic",
            game_url="https://games.evolution.com/slots/synthetic/",
            requested_spins=requested,
            successful_spins=successful,
            failed_spins=max(0, requested - successful),
            status="PARCIAL",
            error=error,
            attempts=attempts,
        )

    def test_completed_actions_are_ok_when_only_gap_is_settings_game_modes(self) -> None:
        result = self._result(
            attempts=[
                SpinAttempt(number=1, ok=True, mode_id="SPIN"),
                SpinAttempt(number=1, ok=True, mode_id="PURCHASE_FREESPINS", mode_kind="PURCHASE"),
            ],
            successful=2,
            requested=2,
            error=(
                "Red Tiger respondió 2/2; validados=2/2. "
                "Cobertura pendiente: GAME_MODES_WIRE_CONTRACT."
            ),
        )

        _normalize_metadata_only_partial(result)

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.error, "")

    def test_real_failed_action_remains_partial(self) -> None:
        result = self._result(
            attempts=[
                SpinAttempt(number=1, ok=True, mode_id="SPIN"),
                SpinAttempt(number=1, ok=False, mode_id="PURCHASE_SUPERFREESPINS", mode_kind="PURCHASE"),
            ],
            successful=1,
            requested=2,
            error=(
                "Red Tiger respondió 1/2; validados=1/2. "
                "Cobertura pendiente: GAME_MODES_WIRE_CONTRACT. "
                "Errores: success=False."
            ),
        )

        _normalize_metadata_only_partial(result)

        self.assertEqual(result.status, "PARCIAL")
        self.assertIn("Errores:", result.error)

    def test_other_coverage_gap_is_not_hidden(self) -> None:
        result = self._result(
            attempts=[SpinAttempt(number=1, ok=True, mode_id="SPIN")],
            successful=1,
            requested=1,
            error=(
                "Red Tiger respondió 1/1; validados=1/1. "
                "Cobertura pendiente: FEATURE_BUY_CONTRACT, GAME_MODES_WIRE_CONTRACT."
            ),
        )

        _normalize_metadata_only_partial(result)

        self.assertEqual(result.status, "PARCIAL")
        self.assertIn("FEATURE_BUY_CONTRACT", result.error)


if __name__ == "__main__":
    unittest.main()
