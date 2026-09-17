from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from tester_spin.feature_sessions import (
    FEATURE_COMPLETE,
    FEATURE_INCOMPLETE,
    FEATURE_NOT_OBSERVED,
    FEATURE_UNKNOWN,
    attach_feature_session_report,
    enforce_complete_feature_sessions,
    feature_session_state_for_mode,
    finalize_feature_session_report,
    make_feature_choice,
    make_feature_round,
    make_feature_session,
)
from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import ProviderAdapter


class FeatureSessionCoreTests(unittest.TestCase):
    @staticmethod
    def _result(root: Path, *, status: str = "OK") -> GameTestResult:
        return GameTestResult(
            provider="synthetic",
            slug="synthetic",
            game_name="Synthetic",
            game_url="https://example.invalid/game",
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status=status,
            run_dir=str(root),
        )

    def test_unresolved_choice_domain_keeps_observed_feature_incomplete(self) -> None:
        choice = make_feature_choice(
            command="pick",
            prefix=[],
            required_options=["DOMAIN_UNRESOLVED"],
            covered_options=[],
            domain_state="UNRESOLVED",
            source="synthetic-wire",
        )
        session = make_feature_session(
            session_id="PURCHASE_PICK:1",
            trigger="PURCHASE",
            parent_mode="PURCHASE_PICK",
            attempt_number=1,
            entry={"command": "buy_feature"},
            rounds=[make_feature_round(1, provider_action="freespin", source="wire_response")],
            choices=[choice],
            terminal_proven=True,
            returned_to_base=True,
            wire_steps=3,
            round_classification_complete=True,
        )

        self.assertEqual(session["state"], FEATURE_INCOMPLETE)
        self.assertFalse(session["choice_coverage_complete"])
        self.assertIn("choice", " ".join(session["reasons"]).lower())

    def test_complete_feature_requires_terminal_rounds_and_closed_choice_graph(self) -> None:
        choice = make_feature_choice(
            command="select",
            prefix=[],
            required_options=["0", "1"],
            covered_options=["0", "1"],
            domain_state="PROVEN",
            source="provider-domain",
            required_samples=1,
            sample_counts={"0": 1, "1": 1},
        )
        session = make_feature_session(
            session_id="PURCHASE_FREESPIN:1",
            trigger="PURCHASE",
            parent_mode="PURCHASE_FREESPIN",
            attempt_number=1,
            entry={"command": "buy_feature"},
            rounds=[
                make_feature_round(1, provider_action="freespin", wire_step=2, source="wire_response"),
                make_feature_round(2, provider_action="freespin", wire_step=3, source="wire_response"),
            ],
            choices=[choice],
            terminal_proven=True,
            returned_to_base=True,
            wire_steps=4,
            round_classification_complete=True,
        )

        self.assertEqual(session["state"], FEATURE_COMPLETE)
        self.assertEqual(session["totals"]["logical_rounds"], 2)
        self.assertEqual(session["totals"]["wire_steps"], 4)
        self.assertEqual(session["totals"]["choices"], 1)

    def test_parent_modes_are_aggregated_independently(self) -> None:
        complete = make_feature_session(
            session_id="PURCHASE_A:1",
            trigger="PURCHASE",
            parent_mode="PURCHASE_A",
            attempt_number=1,
            entry={"command": "buy"},
            rounds=[make_feature_round(1, provider_action="round", source="wire")],
            terminal_proven=True,
            returned_to_base=True,
            wire_steps=2,
        )
        unknown = make_feature_session(
            session_id="PURCHASE_B:1",
            trigger="PURCHASE",
            parent_mode="PURCHASE_B",
            attempt_number=1,
            entry={"command": "buy"},
            rounds=[],
            terminal_proven=False,
            returned_to_base=False,
            wire_steps=2,
            evidence_state="UNKNOWN",
            reasons=["provider state observed but round semantics unresolved"],
        )
        result = self._result(Path("."))
        report = finalize_feature_session_report(
            result,
            sessions=[complete, unknown],
            authority="synthetic",
        )

        self.assertEqual(report["by_parent_mode"]["PURCHASE_A"]["state"], FEATURE_COMPLETE)
        self.assertEqual(report["by_parent_mode"]["PURCHASE_B"]["state"], FEATURE_UNKNOWN)
        self.assertFalse(report["complete"])

    def test_incomplete_feature_downgrades_ok_result_and_persists_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self._result(root)
            session = make_feature_session(
                session_id="SPIN:1",
                trigger="NATURAL",
                parent_mode="SPIN",
                attempt_number=1,
                entry={"command": "spin"},
                rounds=[make_feature_round(1, provider_action="freespin", source="wire")],
                terminal_proven=False,
                returned_to_base=False,
                wire_steps=2,
                reasons=["feature did not return to base"],
            )
            report = finalize_feature_session_report(result, sessions=[session], authority="synthetic")

            enforce_complete_feature_sessions(result, report, progress=lambda _message: None)

            self.assertEqual(result.status, "PARCIAL")
            self.assertIn("feature", result.error.lower())
            artifact = root / "feature-sessions.json"
            self.assertTrue(artifact.is_file())
            saved = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual(saved["sessions"][0]["state"], FEATURE_INCOMPLETE)
            self.assertEqual(feature_session_state_for_mode(result, "SPIN"), FEATURE_INCOMPLETE)

    def test_no_observed_session_is_explicitly_not_observed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self._result(Path(temp))
            report = finalize_feature_session_report(result, sessions=[], authority="synthetic")
            attach_feature_session_report(result, report)
            self.assertTrue(report["complete"])
            self.assertEqual(feature_session_state_for_mode(result, "PURCHASE_NONE"), FEATURE_NOT_OBSERVED)


class _FeatureProvider(ProviderAdapter):
    key = "feature-test"
    display_name = "Feature Test"
    catalog_url = "https://example.invalid/catalog"

    def crawl_catalog(self, *, stop_event, progress, max_pages=100, on_game=None):
        return []

    def test_game(self, game, *, spins, timeout_s, stop_event, progress):
        raise AssertionError("not used")

    def build_feature_sessions(self, result: GameTestResult) -> dict:
        session = make_feature_session(
            session_id="PURCHASE_PICK:1",
            trigger="PURCHASE",
            parent_mode="PURCHASE_PICK",
            attempt_number=1,
            entry={"command": "buy"},
            rounds=[make_feature_round(1, provider_action="round", source="wire")],
            choices=[
                make_feature_choice(
                    command="pick",
                    required_options=["DOMAIN_UNRESOLVED"],
                    covered_options=[],
                    domain_state="UNRESOLVED",
                )
            ],
            terminal_proven=True,
            returned_to_base=True,
            wire_steps=3,
        )
        return finalize_feature_session_report(result, sessions=[session], authority="feature-test")


class ProviderFeatureFinalizerTests(unittest.TestCase):
    def test_purchase_finalizer_runs_feature_gate_without_general_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = _FeatureProvider(Path(temp) / "data")
            result = FeatureSessionCoreTests._result(Path(temp) / "run")

            finalized = provider.finalize_purchase_result(
                result,
                progress=lambda _message: None,
            )

            self.assertIs(finalized, result)
            self.assertEqual(result.status, "PARCIAL")
            self.assertEqual(
                feature_session_state_for_mode(result, "PURCHASE_PICK"),
                FEATURE_INCOMPLETE,
            )
            self.assertTrue(Path(result.run_dir, "feature-sessions.json").is_file())
            self.assertFalse(Path(result.run_dir, "sample-catalog.json").is_file())


if __name__ == "__main__":
    unittest.main()
