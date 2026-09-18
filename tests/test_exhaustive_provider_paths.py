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
    PragmaticProvider,
    RubyPlayProvider,
)
from tester_spin.providers import bgaming_exhaustive, belatra_exhaustive, pragmatic_exhaustive
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
        self.assertEqual(PragmaticProvider.__module__, "tester_spin.providers.pragmatic_hybrid")
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
            mode = result.discovered_modes[-1]
            self.assertEqual(mode["parent"], "SPIN")
            self.assertEqual(mode["id"], "SPIN__SELECT_INDEX_DOMAIN")
            self.assertIn("DOMAIN_UNRESOLVED", mode["required_options"])
            self.assertEqual(mode["observed_indices"], [0])

    def test_rubyplay_picker_domains_are_separate_for_each_parent_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "PURCHASE_SELECT" / "attempt-001"
            second = root / "PURCHASE_PICK" / "attempt-001"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "step-002-request.json").write_text(
                json.dumps({"action": "select", "index": 0}),
                encoding="utf-8",
            )
            (second / "step-002-request.json").write_text(
                json.dumps({"action": "pick", "index": 3}),
                encoding="utf-8",
            )
            result = self._result(root, "rubyplay")

            apply_rubyplay_path_audit(result, progress=lambda _message: None)

            indexed = {
                (str(item.get("parent")), str(item.get("wire_command"))): item
                for item in result.discovered_modes
                if isinstance(item, dict)
                and str(item.get("kind") or "") == "INDEXED_CHOICE"
            }
            self.assertEqual(indexed[("PURCHASE_SELECT", "select")]["observed_indices"], [0])
            self.assertEqual(indexed[("PURCHASE_PICK", "pick")]["observed_indices"], [3])
            self.assertNotIn(("PURCHASE_SELECT", "pick"), indexed)
            self.assertNotIn(("PURCHASE_PICK", "select"), indexed)

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

    def test_bgaming_flow_choice_mode_materializes_purchase_scope(self) -> None:
        mode = bgaming_exhaustive._choice_mode_from_point(
            {
                "scope": "PURCHASE_FUTURE_FEATURE_LEVEL_0",
                "command": "pick_cards",
                "option_field": "mode",
                "source": "client-proven",
                "prefix": ("mode=select",),
                "available": ("mode=select", "mode=auto"),
                "sample_counts": {"mode=select": 1, "mode=auto": 0},
            },
            repetitions=1,
        )
        self.assertEqual(mode["scope"], "PURCHASE_FUTURE_FEATURE_LEVEL_0")
        self.assertEqual(mode["parent"], "PURCHASE_FUTURE_FEATURE_LEVEL_0")
        self.assertTrue(mode["coverage_required"])
        self.assertEqual(mode["required_options"], ["mode=select", "mode=auto"])
        self.assertEqual(mode["covered_options"], ["mode=select"])

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

    def test_pragmatic_branch_points_are_keyed_by_selector_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            attempt_root = Path(temp) / "PURCHASE_1" / "attempt-0001"
            attempt_root.mkdir(parents=True)
            (attempt_root / "fso-selection-002.json").write_text(
                json.dumps(
                    {
                        "schema": "tester-spin/pragmatic-fso-selection/v1",
                        "option_indices": [0, 1],
                        "selected_index": 0,
                    }
                ),
                encoding="utf-8",
            )
            (attempt_root / "fso-selection-005.json").write_text(
                json.dumps(
                    {
                        "schema": "tester-spin/pragmatic-fso-selection/v1",
                        "option_indices": [0, 1],
                        "selected_index": 1,
                    }
                ),
                encoding="utf-8",
            )
            attempt = SpinAttempt(
                number=1,
                ok=True,
                terminal=True,
                mode_id="PURCHASE_1",
                artifact_dir=str(attempt_root),
            )
            points = {}
            pragmatic_exhaustive._ingest_attempt(points, attempt)
            self.assertIn(("PURCHASE_1", ()), points)
            self.assertIn(("PURCHASE_1", (0,)), points)
            self.assertEqual(points[("PURCHASE_1", ())].covered, {0})
            self.assertEqual(points[("PURCHASE_1", (0,))].covered, {1})
            self.assertIn(("PURCHASE_1", (1,)), pragmatic_exhaustive._missing_prefixes(points))
            self.assertIn(("PURCHASE_1", (0, 0)), pragmatic_exhaustive._missing_prefixes(points))

    def test_pragmatic_forced_prefix_selects_requested_then_discovers_first_sibling(self) -> None:
        state_before = getattr(pragmatic_exhaustive._FORCE_LOCAL, "state", None)
        try:
            pragmatic_exhaustive._FORCE_LOCAL.state = {
                "prefix": (1,),
                "depth": 0,
                "trace": [],
            }
            options = [{"index": 0}, {"index": 1}, {"index": 2}]
            first = pragmatic_exhaustive._forced_choose_fs_option_index(
                options,
                repetition=99,
            )
            second = pragmatic_exhaustive._forced_choose_fs_option_index(
                options,
                repetition=99,
            )
            self.assertEqual(first, 1)
            self.assertEqual(second, 0)
        finally:
            pragmatic_exhaustive._FORCE_LOCAL.state = state_before


if __name__ == "__main__":
    unittest.main()
