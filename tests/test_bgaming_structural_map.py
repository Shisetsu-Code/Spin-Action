from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.bgaming.runtime import BGamingRuntime, post_command
from tester_spin.providers.bgaming.server_guided import begin_dynamic_contract_run, end_dynamic_contract_run, remember_dynamic_evidence
from tester_spin.providers.bgaming.structural_map import begin_capture, end_capture, record_discovery
from tester_spin.providers.bgaming.ui_index_domain import augment_evidence_with_ui_index_domain
from tester_spin.run_result_publisher import build_result_document
from test_bgaming_ui_index_domain import _bundle, _data, _evidence


def runtime():
    session = Mock()
    return BGamingRuntime(session, "https://example.test/?token=private", "https://example.test/api",
                          "fixture", "X-CSRF", "private-csrf", {}, 123456)


def send(active, command, data, options=None):
    response = Mock()
    response.json.return_value = data
    active.session.post.return_value = response
    return post_command(active, command, timeout_s=1, options=options)


class BGamingStructuralMapTests(unittest.TestCase):
    def test_real_transport_builds_map_and_result_publisher_embeds_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "tests" / "run-a"
            run.mkdir(parents=True)
            capture, token = begin_capture("generic-fixture")
            begin_dynamic_contract_run()
            try:
                active = runtime()
                send(active, "init", {"flow": {"state": "closed", "available_actions": ["spin"]}})
                send(active, "spin", {"flow": {"state": "gamble_bonus", "available_actions": ["gamble_bonus"]}}, {"purchased_feature": "freespins"})
                send(active, "gamble_bonus", _data())
                evidence = augment_evidence_with_ui_index_domain(_data(), _evidence(), _bundle())
                evidence["client"] = {"bundle_sha256": "a" * 64}
                remember_dynamic_evidence(evidence)
                record_discovery(_data(), evidence)
                send(active, "pick_cards", {"flow": {"state": "freespins", "available_actions": ["freespin"]}}, {"mode": "any", "index": 2})
                result = GameTestResult("bgaming", "generic-fixture", "Generic", "", 1, 1, 0, "OK", run_dir=str(run))
                capture.finish(result, root)
                data = json.loads((run / "analysis/game-structure.json").read_text())
                choices = [c for c in data["choices"].values() if c["request"]["command"] == "pick_cards"]
                self.assertEqual(len(choices), 5)
                self.assertEqual(sum(c["coverage"]["executed"] for c in choices), 1)
                self.assertEqual(sum(c["coverage"]["outcome_observed"] for c in choices), 1)
                self.assertTrue(data["relations"])
                self.assertTrue(data["field_semantics"])
                for edge in data["transitions"].values():
                    self.assertIn(edge["from_state"], data["states"])
                    self.assertIn(edge["choice"], data["choices"])
                    self.assertIn(edge["path_context"], data["path_context"])
                for decision in data["decision_points"].values():
                    for prefix in decision["replay_prefixes"]:
                        self.assertTrue(all(key in data["choices"] for key in prefix))
                document = build_result_document(result)
                self.assertIn("analysis/game-structure.json", document["artifacts"])
                self.assertEqual(document["result"]["structural_map"]["schema"], data["schema"])
                self.assertNotIn("private", json.dumps(data))
            finally:
                end_dynamic_contract_run()
                end_capture(token)

    def test_transport_error_preserves_attempt_without_outcome(self):
        capture, token = begin_capture("failed")
        try:
            active = runtime()
            active.session.post.side_effect = requests.ConnectionError("private credentials must not leak")
            with self.assertRaises(requests.ConnectionError):
                post_command(active, "pick_cards", options={"mode": "auto"}, timeout_s=1)
            choice = next(iter(capture.graph.data["choices"].values()))
            self.assertTrue(choice["coverage"]["executed"])
            self.assertFalse(choice["coverage"]["outcome_observed"])
            self.assertFalse(capture.graph.data["transitions"])
        finally:
            end_capture(token)

    def test_observer_failure_does_not_break_wire(self):
        capture, token = begin_capture("failed-observer")
        try:
            with patch.object(capture.graph, "observe", side_effect=ValueError("private")):
                result = send(runtime(), "init", _data())
            self.assertEqual(result[2], _data())
            self.assertEqual(capture.diagnostics, ["wire capture: ValueError"])
        finally:
            end_capture(token)

    def test_new_init_resets_prefix_and_prevents_cross_session_edge(self):
        capture, token = begin_capture("sessions")
        try:
            active = runtime()
            send(active, "init", _data())
            send(active, "pick_cards", {"flow": {"state": "freespins"}}, {"index": 0})
            send(active, "init", _data())
            init_choices = [c for c in capture.graph.data["choices"].values() if c["request"]["command"] == "init"]
            self.assertEqual(len(init_choices), 1)
            self.assertEqual(init_choices[0]["coverage"]["attempts"], 2)
            self.assertEqual(len(capture.prefix), 1)
        finally:
            end_capture(token)

    def test_concurrent_capture_contexts_are_isolated(self):
        def collect(slug):
            capture, token = begin_capture(slug)
            try:
                send(runtime(), "init", _data())
                return capture.graph.to_dict()
            finally:
                end_capture(token)
        with ThreadPoolExecutor(2) as pool:
            a, b = pool.map(collect, ["one", "two"])
        self.assertFalse(set(a["choices"]) & set(b["choices"]))

    def test_client_fingerprint_mismatch_does_not_publish_mixed_graph(self):
        capture, token = begin_capture("incompatible")
        try:
            with tempfile.TemporaryDirectory() as directory:
                capture.fingerprints.update(["one", "two"])
                result = GameTestResult("bgaming", "incompatible", "", "", 1, 0, 1, "OK", run_dir=directory)
                with self.assertRaises(ValueError):
                    capture.finish(result, Path(directory))
                self.assertFalse((Path(directory) / "analysis/game-structure.json").exists())
        finally:
            end_capture(token)

    def test_provider_wrapper_persists_and_reports_map_failure(self):
        import tester_spin.providers.bgaming_paths_v2 as paths
        game = Game("bgaming", "wrapper", "", "")
        with tempfile.TemporaryDirectory() as directory:
            result = GameTestResult("bgaming", "wrapper", "", "", 1, 1, 0, "OK", run_dir=directory)
            with patch.object(paths._exhaustive.BGamingProvider, "test_game", return_value=result), \
                 patch.object(paths._policy, "finalize_policy_artifacts", side_effect=lambda value: value), \
                 patch("tester_spin.providers.bgaming.structural_map.Capture.finish", side_effect=ValueError("private")):
                provider = object.__new__(paths.BGamingProvider)
                provider.game_dir = lambda game: Path(directory)
                observed = provider.test_game(game, spins=1, timeout_s=1, stop_event=Mock(), progress=Mock())
            self.assertEqual(observed.status, "PARCIAL")
            self.assertEqual(observed.structural_map["diagnostic"], "ValueError")
            self.assertNotIn("private", observed.error)


if __name__ == "__main__":
    unittest.main()
