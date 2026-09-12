from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers import (
    BGamingProvider,
    BelatraProvider,
    OneSpin4WinProvider,
    RubyPlayProvider,
)
from tester_spin.providers import bgaming_exhaustive, belatra_exhaustive
from tester_spin.providers.one_spin4win_exhaustive import apply_d1_path_audit
from tester_spin.providers.rubyplay.exhaustive import apply_rubyplay_path_audit


class ExhaustiveProviderPathTests(unittest.TestCase):
    @staticmethod
    def _result(root: Path, provider: str) -> GameTestResult:
        return GameTestResult(
            provider=provider,
            slug="synthetic",
            game_name="Synthetic",
            game_url="https://example.invalid/game",
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
            run_dir=str(root),
        )

    def test_active_provider_wrappers_are_wired(self) -> None:
        self.assertEqual(BGamingProvider.__module__, "tester_spin.providers.bgaming")
        self.assertEqual(BelatraProvider.__module__, "tester_spin.providers.belatra_exhaustive")
        self.assertEqual(OneSpin4WinProvider.__module__, "tester_spin.providers.one_spin4win_exhaustive")
        self.assertEqual(RubyPlayProvider.__module__, "tester_spin.providers.rubyplay.exhaustive")

    def test_belatra_discovers_full_scalar_selector_matrix(self) -> None:
        gs = {
            "vipMode": {"on": 0, "vipBetK": 2},
            "isMathElf": 0,
            "mathType": 1,
            "analInfo": {"mathTypeClassic": 1, "mathTypeHigh": 2},
        }
        dimensions = belatra_exhaustive._selector_dimensions(gs)
        self.assertEqual(
            dimensions,
            [
                ("vipOn", [0, 1]),
                ("isMathElf", [0, 1]),
                ("mathType", [1, 2]),
            ],
        )
        matrix = belatra_exhaustive._selector_matrix(dimensions)
        self.assertEqual(len(matrix), 8)
        self.assertIn({"vipOn": 1, "isMathElf": 1, "mathType": 2}, matrix)

    def test_belatra_buy_bonus_is_detected_without_guessing_wire(self) -> None:
        self.assertEqual(
            belatra_exhaustive._buy_bonus_options(
                {"buyBonus": {"buyTotalBetK": {"bonusA": 50, "bonusB": 100}}}
            ),
            ["bonusA", "bonusB"],
        )

    def test_rubyplay_indexed_choice_cannot_be_called_exhaustive_without_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = root / "SPIN" / "attempt-001"
            attempt.mkdir(parents=True)
            (attempt / "step-002-request.json").write_text(
                json.dumps({"action": "select", "index": 0}),
                encoding="utf-8",
            )
            result = self._result(root, "rubyplay")
            apply_rubyplay_path_audit(result, progress=lambda _message: None)
            self.assertEqual(result.status, "PARCIAL")
            self.assertIn("DOMAIN_UNRESOLVED", result.discovered_modes[-1]["required_options"])
            self.assertEqual(result.discovered_modes[-1]["observed_indices"], [0])

    def test_d1_unknown_result_state_is_never_treated_as_terminal_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = root / "SPIN" / "attempt-001"
            attempt.mkdir(parents=True)
            (attempt / "ws-attempt.json").write_text(
                json.dumps(
                    {
                        "frames": [
                            {
                                "direction": "received",
                                "payload": {
                                    "kind": "text",
                                    "text": json.dumps({"type": "3", "st": 99}),
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = self._result(root, "1spin4win")
            result.attempts.append(
                SpinAttempt(
                    number=1,
                    ok=True,
                    mode_id="SPIN",
                    terminal=True,
                    artifact_dir=str(attempt),
                )
            )
            provider = OneSpin4WinProvider(root / "data")
            apply_d1_path_audit(provider, result, progress=lambda _message: None)
            self.assertEqual(result.status, "PARCIAL")
            self.assertIn("99", result.error)

    def test_bgaming_cartesian_product_covers_every_client_proven_option_tuple(self) -> None:
        profile = {
            "spin_options": {"mode": "20", "variant": "normal"},
            "spin_option_choices": {
                "mode": ["20", "40"],
                "variant": ["normal", "super"],
            },
        }
        domains = bgaming_exhaustive._choice_domains(profile)
        matrix = bgaming_exhaustive._matrix(domains)
        self.assertEqual(len(matrix), 4)
        self.assertIn({"mode": "40", "variant": "super"}, matrix)
        self.assertEqual(
            bgaming_exhaustive._base_combo(profile, domains),
            {"mode": "20", "variant": "normal"},
        )

    def test_bgaming_profile_override_accepts_only_discovered_domain(self) -> None:
        profile = SimpleNamespace(
            spin_option_choices={"mode": ["20", "40"]},
            spin_options={"mode": "20"},
            evidence=[],
        )
        old = getattr(bgaming_exhaustive._OVERRIDE_LOCAL, "spin_options", None)
        try:
            bgaming_exhaustive._OVERRIDE_LOCAL.spin_options = {"mode": "40"}
            updated = bgaming_exhaustive._apply_profile_override(profile)
            self.assertEqual(updated.spin_options["mode"], "40")
            with self.assertRaises(ValueError):
                bgaming_exhaustive._OVERRIDE_LOCAL.spin_options = {"mode": "999"}
                bgaming_exhaustive._apply_profile_override(profile)
        finally:
            bgaming_exhaustive._OVERRIDE_LOCAL.spin_options = old


if __name__ == "__main__":
    unittest.main()
