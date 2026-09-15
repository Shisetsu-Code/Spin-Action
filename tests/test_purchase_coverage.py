from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.purchase_coverage import (
    NO_PURCHASE_PROVEN,
    PURCHASE_COMPLETE,
    PURCHASE_FAILED,
    PURCHASE_UNKNOWN,
    aggregate_purchase_coverages,
    finalize_purchase_coverage,
    make_purchase_option,
)
from tester_spin.providers.belatra_purchase_coverage import build_belatra_purchase_coverage
from tester_spin.providers.one_spin4win_purchase_coverage import build_one_spin4win_purchase_coverage
from tester_spin.providers.pragmatic_purchase_coverage import build_pragmatic_purchase_coverage
from tester_spin.providers.redtiger.purchase_coverage import build_redtiger_purchase_coverage
from tester_spin.providers.rubyplay.purchase_coverage import build_rubyplay_purchase_coverage


def _result(
    provider: str,
    *,
    modes: list[dict] | None = None,
    attempts: list[SpinAttempt] | None = None,
    run_dir: str = "",
    inventory_state: str | None = "COMPLETE",
) -> GameTestResult:
    structural = {}
    if inventory_state is not None:
        structural["action_inventory"] = {
            "state": inventory_state,
            "source": f"{provider}-authoritative-runtime",
            "reason": "synthetic authoritative inventory",
        }
    return GameTestResult(
        provider=provider,
        slug="synthetic",
        game_name="Synthetic",
        game_url="https://example.invalid/synthetic",
        requested_spins=max(1, len(attempts or [])),
        successful_spins=sum(1 for item in attempts or [] if item.ok),
        failed_spins=sum(1 for item in attempts or [] if not item.ok),
        status="OK",
        symbol="synthetic-id",
        discovered_modes=list(modes or []),
        run_dir=run_dir,
        attempts=list(attempts or []),
        structural_map=structural,
    )


def _attempt(root: Path, mode_id: str, request: dict, *, ok: bool = True, terminal: bool = True, warning: str = "", error: str = "") -> SpinAttempt:
    attempt_dir = root / mode_id / "attempt-00001"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "request.json").write_text(json.dumps(request), encoding="utf-8")
    return SpinAttempt(
        number=1,
        ok=ok,
        mode_id=mode_id,
        mode_kind="PURCHASE",
        status_code=200,
        terminal=terminal,
        warning=warning,
        error=error,
        artifact_dir=str(attempt_dir),
    )


class PurchaseCoverageCoreTests(unittest.TestCase):
    def test_metadata_only_candidate_never_promotes(self) -> None:
        result = _result("demo")
        option = make_purchase_option(
            "PURCHASE_META",
            provider_selector={"candidate": "x100"},
            source_kind="metadata",
            executable=False,
            wire_contract_state="UNKNOWN",
            execution_state="NOT_ATTEMPTED",
            terminal=False,
            reason="metadata only",
        )
        coverage = finalize_purchase_coverage(
            result,
            options=[option],
            inventory_state="COMPLETE",
            authority="metadata-only-test",
        )
        self.assertEqual(coverage["state"], PURCHASE_UNKNOWN)

    def test_no_purchase_requires_authoritative_absence(self) -> None:
        result = _result("demo")
        unknown = finalize_purchase_coverage(
            result,
            options=[],
            inventory_state="COMPLETE",
            authority="runtime",
            no_purchase_proven=False,
        )
        proven = finalize_purchase_coverage(
            result,
            options=[],
            inventory_state="COMPLETE",
            authority="runtime",
            no_purchase_proven=True,
        )
        self.assertEqual(unknown["state"], PURCHASE_UNKNOWN)
        self.assertEqual(proven["state"], NO_PURCHASE_PROVEN)

    def test_every_authoritative_option_must_complete(self) -> None:
        result = _result("demo")
        completed = make_purchase_option(
            "PURCHASE_A",
            provider_selector={"id": "a"},
            source_kind="runtime",
            executable=True,
            wire_contract_state="PROVEN",
            execution_state="COMPLETE",
            terminal=True,
        )
        unresolved = make_purchase_option(
            "PURCHASE_B",
            provider_selector={"id": "b"},
            source_kind="runtime",
            executable=True,
            wire_contract_state="PROVEN",
            execution_state="NOT_ATTEMPTED",
            terminal=False,
        )
        coverage = finalize_purchase_coverage(
            result,
            options=[completed, unresolved],
            inventory_state="COMPLETE",
            authority="runtime",
        )
        self.assertEqual(coverage["state"], PURCHASE_UNKNOWN)
        self.assertEqual(coverage["counts"]["complete"], 1)
        self.assertEqual(coverage["counts"]["unknown"], 1)

    def test_proven_wire_attempt_that_fails_is_purchase_failed(self) -> None:
        result = _result("demo")
        failed = make_purchase_option(
            "PURCHASE_A",
            provider_selector={"id": "a"},
            source_kind="runtime",
            executable=True,
            wire_contract_state="PROVEN",
            execution_state="FAILED",
            terminal=False,
            reason="server rejected action",
        )
        coverage = finalize_purchase_coverage(
            result,
            options=[failed],
            inventory_state="COMPLETE",
            authority="runtime",
        )
        self.assertEqual(coverage["state"], PURCHASE_FAILED)

    def test_aggregate_is_fail_closed(self) -> None:
        rows = [
            {"state": PURCHASE_COMPLETE, "options": [{}]},
            {"state": NO_PURCHASE_PROVEN, "options": []},
            {"state": PURCHASE_UNKNOWN, "options": [{}]},
        ]
        aggregate = aggregate_purchase_coverages(rows)
        self.assertEqual(aggregate["overall_state"], PURCHASE_UNKNOWN)
        self.assertEqual(aggregate["counts"][PURCHASE_COMPLETE], 1)
        self.assertEqual(aggregate["counts"][NO_PURCHASE_PROVEN], 1)
        self.assertEqual(aggregate["counts"][PURCHASE_UNKNOWN], 1)


class PragmaticPurchaseCoverageTests(unittest.TestCase):
    def test_exact_pur_selector_and_terminal_attempt_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mode = {
                "id": "PURCHASE_1",
                "kind": "PURCHASE",
                "enabled": True,
                "provider_pur": 0,
                "price_known": True,
                "paid_cost": 200.0,
            }
            attempt = _attempt(root, "PURCHASE_1", {"action": "doSpin", "pur": "0"})
            result = _result("pragmatic", modes=[mode], attempts=[attempt], run_dir=str(root))
            coverage = build_pragmatic_purchase_coverage(result)
            self.assertEqual(coverage["state"], PURCHASE_COMPLETE)
            self.assertEqual(coverage["options"][0]["provider_selector"], {"pur": 0})

    def test_http_200_with_wrong_pur_is_not_a_false_positive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mode = {
                "id": "PURCHASE_1",
                "kind": "PURCHASE",
                "enabled": True,
                "provider_pur": 0,
                "price_known": True,
                "paid_cost": 200.0,
            }
            attempt = _attempt(root, "PURCHASE_1", {"action": "doSpin", "pur": "1"})
            result = _result("pragmatic", modes=[mode], attempts=[attempt], run_dir=str(root))
            coverage = build_pragmatic_purchase_coverage(result)
            self.assertEqual(coverage["state"], PURCHASE_UNKNOWN)

    def test_closed_doinit_inventory_can_prove_no_purchase(self) -> None:
        coverage = build_pragmatic_purchase_coverage(_result("pragmatic"))
        self.assertEqual(coverage["state"], NO_PURCHASE_PROVEN)


class RubyPlayPurchaseCoverageTests(unittest.TestCase):
    def test_exact_buy_feature_type_and_price_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mode = {
                "id": "PURCHASE_FREESPIN",
                "kind": "PURCHASE",
                "observed": True,
                "executable": True,
                "wire_command": "buy_feature",
                "buy_feature_type": "freespin",
                "feature_multiplier": 100.0,
                "default_price": 1000.0,
            }
            attempt = _attempt(
                root,
                "PURCHASE_FREESPIN",
                {"action": "buy_feature", "buy_feature_type": "freespin", "buy_feature_price": 1000.0},
            )
            result = _result("rubyplay", modes=[mode], attempts=[attempt], run_dir=str(root))
            coverage = build_rubyplay_purchase_coverage(result)
            self.assertEqual(coverage["state"], PURCHASE_COMPLETE)

    def test_warning_or_mismatched_type_cannot_promote(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mode = {
                "id": "PURCHASE_FREESPIN",
                "kind": "PURCHASE",
                "observed": True,
                "executable": True,
                "wire_command": "buy_feature",
                "buy_feature_type": "freespin",
                "feature_multiplier": 100.0,
                "default_price": 1000.0,
            }
            attempt = _attempt(
                root,
                "PURCHASE_FREESPIN",
                {"action": "buy_feature", "buy_feature_type": "respin", "buy_feature_price": 1000.0},
                warning="returned feature type mismatch",
            )
            result = _result("rubyplay", modes=[mode], attempts=[attempt], run_dir=str(root))
            coverage = build_rubyplay_purchase_coverage(result)
            self.assertNotEqual(coverage["state"], PURCHASE_COMPLETE)

    def test_closed_client_init_inventory_can_prove_no_purchase(self) -> None:
        coverage = build_rubyplay_purchase_coverage(_result("rubyplay"))
        self.assertEqual(coverage["state"], NO_PURCHASE_PROVEN)


class RedTigerPurchaseCoverageTests(unittest.TestCase):
    def test_settings_feature_and_exact_request_extras_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game_dir = root / "game"
            game_dir.mkdir()
            (game_dir / "game.json").write_text(
                json.dumps(
                    {
                        "has_feature_buy": True,
                        "feature_buys": [{"name": "FREESPINS", "multiplier": "100"}],
                    }
                ),
                encoding="utf-8",
            )
            mode = {
                "id": "PURCHASE_FREESPINS",
                "kind": "PURCHASE",
                "observed": True,
                "executable": True,
                "feature_buy": "FREESPINS",
                "feature_multiplier": "100",
                "cost": "100",
            }
            attempt = _attempt(
                root,
                "PURCHASE_FREESPINS",
                {"extras": {"features": {"featureBuy": "FREESPINS", "featureBuyCost": "100"}}},
            )
            result = _result("redtiger", modes=[mode], attempts=[attempt], run_dir=str(root))
            coverage = build_redtiger_purchase_coverage(result, game_dir)
            self.assertEqual(coverage["state"], PURCHASE_COMPLETE)

    def test_has_feature_buy_without_concrete_options_remains_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp)
            (game_dir / "game.json").write_text(
                json.dumps({"has_feature_buy": True, "feature_buys": []}),
                encoding="utf-8",
            )
            coverage = build_redtiger_purchase_coverage(_result("redtiger"), game_dir)
            self.assertEqual(coverage["state"], PURCHASE_UNKNOWN)

    def test_explicit_settings_absence_can_prove_no_purchase(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp)
            (game_dir / "game.json").write_text(
                json.dumps({"has_feature_buy": False, "feature_buys": []}),
                encoding="utf-8",
            )
            coverage = build_redtiger_purchase_coverage(_result("redtiger"), game_dir)
            self.assertEqual(coverage["state"], NO_PURCHASE_PROVEN)


class UnresolvedProviderPurchaseCoverageTests(unittest.TestCase):
    def test_belatra_buy_metadata_is_never_executable_without_wire_mapping(self) -> None:
        mode = {
            "id": "BELATRA_BUY_BONUS",
            "kind": "PURCHASE_BRANCH",
            "observed": True,
            "executable": False,
            "coverage_required": True,
            "required_options": ["0", "1"],
            "covered_options": [],
            "reason": "buyTotalBetK advertised; wire contract unresolved",
        }
        coverage = build_belatra_purchase_coverage(_result("belatra", modes=[mode]))
        self.assertEqual(coverage["state"], PURCHASE_UNKNOWN)
        self.assertEqual(len(coverage["options"]), 2)
        self.assertTrue(all(not option["executable"] for option in coverage["options"]))

    def test_one_spin_client_methods_do_not_prove_purchase_semantics(self) -> None:
        result = _result("1spin4win", inventory_state=None)
        result.structural_map["client_action_evidence"] = {
            "complete": True,
            "aggregate": {"game_controller_methods": ["connect", "playGame", "gamble"]},
        }
        coverage = build_one_spin4win_purchase_coverage(result)
        self.assertEqual(coverage["state"], PURCHASE_UNKNOWN)
        self.assertNotEqual(coverage["state"], NO_PURCHASE_PROVEN)


if __name__ == "__main__":
    unittest.main()
