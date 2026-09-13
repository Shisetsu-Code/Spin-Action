from __future__ import annotations

import json
import tempfile
import threading
import unittest
import requests
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tester_spin.models import Game, GameTestResult, SpinAttempt
from tester_spin.providers import bgaming_exhaustive as bg
from tester_spin.providers import pragmatic_exhaustive as pp
from tester_spin.providers.path_coverage import build_path_coverage_report
from tester_spin.run_result_publisher import build_result_document
from tester_spin.sample_catalog import build_sample_catalog
from tester_spin.providers.bgaming.runtime import http_error_evidence
from tester_spin.providers.base import ProviderAdapter


def result(root: Path, provider="pragmatic"):
    return GameTestResult(provider, "synthetic", "Synthetic", "https://example.invalid", 1, 1, 0, "OK", run_dir=str(root))


def fso(root, number, selections, *, steps=None, ok=True):
    folder = root / "PURCHASE_1" / f"attempt-{number:04d}"
    folder.mkdir(parents=True)
    for depth, value in enumerate(selections):
        step = steps[depth] if steps else depth + 1
        (folder / f"fso-selection-{step:03d}.json").write_text(json.dumps({
            "schema": "tester-spin/pragmatic-fso-selection/v1",
            "option_indices": [0, 1], "selected_index": value}), encoding="utf-8")
    (folder / "response.json").write_text('{"na":"s","win":0}', encoding="utf-8")
    return SpinAttempt(number=number, ok=ok, terminal=ok, mode_id="PURCHASE_1", artifact_dir=str(folder))


class SamplingRegressions(unittest.TestCase):
    def test_finalizer_blocks_insufficient_samples_and_persists_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            r = result(root)
            r.attempts = [fso(root, 1, [0]), fso(root, 2, [1])]
            r.samples_per_path = 3
            ProviderAdapter.finalize_test_result(None, r, progress=lambda _: None)
            self.assertEqual(r.status, "PARCIAL")
            self.assertIn("Muestreo pendiente", r.error)
            self.assertEqual(json.loads((root / "result.json").read_text(encoding="utf-8"))["status"], "PARCIAL")
            self.assertTrue((root / "sample-catalog.json").is_file())

    def test_http_failure_preserves_exact_prepared_options_without_session_token(self):
        response = requests.Response()
        response.status_code = 422
        response._content = b'{"errors":[{"code":203,"desc":"invalid_options"}]}'
        response.request = requests.Request("POST", "https://example.invalid/api/Game/123/private-session", json={
            "command": "select_bonus", "options": {"name": "variant_b", "rows": 5},
            "extra_data": {"api_version": 2}, "play_token": "secret-token"}).prepare()
        evidence = http_error_evidence(response)
        self.assertEqual(evidence["request"]["json"]["options"], {"name": "variant_b", "rows": 5})
        self.assertEqual(evidence["request"]["json"]["command"], "select_bonus")
        self.assertEqual(evidence["json"]["errors"][0]["code"], 203)
        self.assertNotIn("secret-token", json.dumps(evidence))
        self.assertNotIn("private-session", evidence["request"]["url"])

    def test_explicit_unknown_domain_is_not_vacuously_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            r = result(Path(temp))
            r.discovered_modes = [{"id": "new_choice", "coverage_required": True, "required_options": []}]
            self.assertFalse(build_path_coverage_report(r)["complete"])

    def test_fso_same_depth_different_parents_do_not_cover_each_other(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            r = result(root)
            r.attempts = [fso(root, 1, [0, 0]), fso(root, 2, [1, 1])]
            points = {p["signature"]: p for p in build_path_coverage_report(r)["branch_points"]}
            self.assertEqual(points["PURCHASE_1:FSO:0"]["missing"], ["1"])
            self.assertEqual(points["PURCHASE_1:FSO:1"]["missing"], ["0"])

    def test_fso_same_prompt_at_different_wire_steps_merges(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            r = result(root)
            r.attempts = [fso(root, 1, [0], steps=[2]), fso(root, 2, [1], steps=[19])]
            report = build_path_coverage_report(r)
            self.assertTrue(report["complete"])
            self.assertEqual(len(report["branch_points"]), 1)

    def test_failed_selection_does_not_cover_a_branch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            r = result(root)
            r.attempts = [fso(root, 1, [0]), fso(root, 2, [1], ok=False)]
            self.assertEqual(build_path_coverage_report(r)["branch_points"][0]["missing"], ["1"])

    def test_required_sample_quota_is_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            r = result(Path(temp))
            r.discovered_modes = [{"id": "branch", "coverage_required": True,
                                   "required_options": ["A", "B"], "covered_options": ["A", "B"],
                                   "required_samples": 3, "sample_counts": {"A": 3, "B": 1}}]
            point = build_path_coverage_report(r)["branch_points"][0]
            self.assertEqual(point["sample_deficits"], {"A": 0, "B": 2})
            self.assertFalse(point["complete"])

    def test_pragmatic_replays_each_leaf_until_sample_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            r = result(root)
            r.attempts = [fso(root, 1, [0, 0])]
            mode = SimpleNamespace(id="PURCHASE_1")
            catalog = SimpleNamespace(enabled=lambda: [mode])
            discovery = SimpleNamespace(init_response={}, symbol="x", cver="1")
            class Provider:
                base_bet = 1
                def _browser_bootstrap(self, *args, **kwargs):
                    return discovery
                def _test_mode_once(self, *args, repetition, **kwargs):
                    prefix = pp._FORCE_LOCAL.state["prefix"]
                    path = list(prefix) + [0] * (2 - len(prefix))
                    return fso(root, repetition, path)
                def _record_last_test_in_game_json(self, *args):
                    pass
            with patch.object(pp, "discover_modes", return_value=catalog):
                pp.expand_pragmatic_fso_paths(Provider(), Game("pragmatic", "x", "X", "https://example.invalid"), r,
                    repetitions=3, timeout_s=1, stop_event=threading.Event(), progress=lambda _: None)
            counts = {}
            for a in r.attempts:
                path = tuple(value for _, value in pp._read_trace(a))
                counts[path] = counts.get(path, 0) + 1
            self.assertEqual(set(counts), {(0, 0), (0, 1), (1, 0), (1, 1)})
            self.assertTrue(all(n >= 3 for n in counts.values()), counts)
            self.assertTrue(build_path_coverage_report(r)["complete"])

    def test_bgaming_purchase_selectors_are_not_cartesian_dimensions(self):
        # Regression from fortune-trio and secret-bar published diagnostics.
        profile = {"spin_option_choices": {"purchased_feature": ["bonus_buy", 1],
                    "purchased_feature_level": [0, 1], "volatility": ["low", "high"]}}
        self.assertEqual(bg._choice_domains(profile), [("volatility", ["low", "high"])])

    def test_matrix_is_bounded_before_materialization(self):
        matrix = bg._matrix([(f"field{i}", list(range(100))) for i in range(12)])
        self.assertEqual(len(matrix), bg.MAX_OPTION_COMBINATIONS + 1)

    def test_choice_trace_uses_sample_counts(self):
        graph = {}
        trace = [{"scope": "SPIN", "command": "select_bonus", "prefix": [], "available": ["A", "B"], "selected": "A"}]
        bg._merge_choice_trace(graph, trace, complete=True)
        self.assertEqual(bg._next_missing_choice(graph, set(), 3), ("SPIN", "select_bonus", ("A",)))
        bg._merge_choice_trace(graph, trace, complete=False)
        self.assertEqual(next(iter(graph.values()))["sample_counts"], {"A": 1})

    def test_colliding_path_labels_cannot_destroy_evidence(self):
        self.assertNotEqual(bg._safe_label("A/B"), bg._safe_label("A_B"))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, target = root / "source", root / "target"
            source.mkdir(); target.mkdir()
            (target / "evidence.raw").write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                bg._move_run(result(source), target)
            self.assertEqual((target / "evidence.raw").read_text(), "keep")
            self.assertTrue(source.is_dir())

    def test_catalog_groups_by_full_choice_path_and_keeps_invalid_samples(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            r = result(root)
            r.attempts = [fso(root, 1, [0, 1]), fso(root, 2, [0, 1]), fso(root, 3, [1, 0], ok=False)]
            catalog = build_sample_catalog(r, samples_per_path=2)
            groups = catalog["groups"]
            self.assertEqual(len(groups), 2)
            self.assertEqual([g["validated_samples"] for g in groups], [2, 0])
            self.assertEqual([g["missing_samples"] for g in groups], [0, 2])
            self.assertTrue(groups[0]["samples"][0]["evidence"][0]["sha256"])
            self.assertFalse(catalog["observed_paths_sampled"])

    def test_catalog_does_not_count_same_artifact_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            r = result(root)
            a = fso(root, 1, [0])
            r.attempts = [a, a]
            catalog = build_sample_catalog(r, samples_per_path=2)
            self.assertEqual(catalog["groups"][0]["validated_samples"], 1)
            self.assertEqual(catalog["diagnostics"][0]["reason"], "duplicate_artifact_directory")

    def test_empty_run_does_not_scan_current_directory(self):
        r = result(Path("."))
        r.run_dir = ""
        with patch.object(Path, "rglob", side_effect=AssertionError("must not scan")):
            self.assertEqual(build_result_document(r)["artifacts"], {})
            self.assertEqual(build_path_coverage_report(r)["branch_points"], [])

    def test_publisher_keeps_catalog_ahead_of_bulk_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "aaa.raw").write_text("x" * 50)
            (root / "sample-catalog.json").write_text('{"groups":[]}')
            with patch("tester_spin.run_result_publisher.MAX_DOCUMENT_SOURCE_BYTES", 55):
                doc = build_result_document(result(root))
            self.assertIn("sample-catalog.json", doc["artifacts"])
            self.assertNotIn("aaa.raw", doc["artifacts"])


if __name__ == "__main__":
    unittest.main()
