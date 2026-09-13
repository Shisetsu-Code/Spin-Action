from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tester_spin.structure import Choice, Observation, State, StructuralMap, atomic_write, persist_map
from tester_spin.structure.contracts import observe_schema


def graph(game="fixture", fingerprint="bundle-1"):
    return StructuralMap("bgaming", "json-flow", game, fingerprint)


def observation(multiplier=5, **overrides):
    event = Observation(State("choose_multiplier", ("choose_multiplier",)), "choose_multiplier",
        {"command": "choose_multiplier", "options": {"multiplier": multiplier},
         "extra_data": {"round_series_id": "private-round", "session_id": "private-session"}},
        {"flow": {"state": "freespins"}, "balance": {"wallet": 100}, "win": None},
        State("freespins", ("spin",)), executed=True, outcome_observed=True,
        context_after={"selected_multiplier": multiplier}, choice_fields=("options.multiplier",))
    return replace(event, **overrides)


class StructuralMapTests(unittest.TestCase):
    def test_create_transition_and_referential_integrity(self):
        value = graph()
        value.observe(observation())
        data = value.to_dict()
        self.assertEqual(data["schema"], "tester-spin/game-structure/v1")
        edge = next(iter(data["transitions"].values()))
        for field, table in [("from_state", "states"), ("to_state", "states"),
                             ("choice", "choices"), ("decision_point", "decision_points"),
                             ("request_contract", "request_contracts"), ("response_contract", "response_contracts")]:
            self.assertIn(edge[field], data[table])

    def test_merge_same_state_and_transition(self):
        a, b = graph(), graph()
        a.observe(observation())
        b.observe(observation())
        a.merge(b)
        self.assertEqual(len(a.data["states"]), 2)
        self.assertEqual(next(iter(a.data["transitions"].values()))["observations"], 2)

    def test_discover_three_execute_only_one(self):
        value = graph()
        event = observation()
        value.discover(event.before, event.command, [Choice(f"x{i}", {"multiplier": i}) for i in (2, 5, 10)])
        value.observe(event)
        self.assertEqual(value.to_dict()["coverage"]["choices"], 3)
        self.assertEqual(value.to_dict()["coverage"]["executed"], 1)
        self.assertEqual(value.to_dict()["coverage"]["pending"], 2)

    def test_merge_disjoint_choices_without_erasing(self):
        a, b = graph(), graph()
        a.observe(observation(2))
        b.observe(observation(10))
        a.merge(b)
        a.merge(graph())
        self.assertEqual(len(a.data["choices"]), 2)
        self.assertEqual(len(a.data["states"]), 2)

    def test_three_runs_cover_all_choices_and_converge(self):
        value = graph()
        for multiplier in (2, 5, 10):
            run = graph()
            run.observe(observation(multiplier))
            value.merge(run)
        self.assertEqual(value.to_dict()["coverage"]["executed"], 3)
        self.assertEqual(len({e["to_state"] for e in value.data["transitions"].values()}), 1)

    def test_failed_request_executed_without_outcome_or_edge(self):
        value = graph()
        value.observe(observation(response=None, after=None, outcome_observed=False))
        coverage = next(iter(value.data["choices"].values()))["coverage"]
        self.assertTrue(coverage["executed"])
        self.assertFalse(coverage["outcome_observed"])
        self.assertEqual(coverage["successful_samples"], 0)
        self.assertFalse(value.data["transitions"])

    def test_runtime_rounds_and_balances_do_not_change_identity(self):
        a, b = graph(), graph()
        event = observation()
        a.observe(event)
        request = dict(event.request, extra_data={"round_series_id": "another", "session_id": "another-session"})
        b.observe(replace(event, request=request, response={"flow": {"state": "freespins"}, "balance": {"wallet": 200}}))
        for table in ("states", "choices", "transitions", "request_contracts", "response_contracts"):
            self.assertEqual(set(a.data[table]), set(b.data[table]))

    def test_request_roles(self):
        value = graph()
        value.observe(observation())
        schema = next(iter(value.data["request_contracts"].values()))["schema"]["properties"]
        self.assertEqual(schema["command"]["role"], "static")
        self.assertEqual(schema["options"]["properties"]["multiplier"]["role"], "choice")
        extra = schema["extra_data"]["properties"]
        self.assertEqual(extra["round_series_id"]["role"], "runtime")
        self.assertEqual(extra["session_id"]["role"], "session")

    def test_response_nullable_optional_counts_across_merge(self):
        a, b = graph(), graph()
        a.observe(observation(response={"x": 3, "optional": True}))
        b.observe(observation(response={"x": None}))
        a.merge(b)
        fields = next(iter(a.data["response_contracts"].values()))["schema"]["properties"]
        self.assertEqual(fields["x"]["observed_types"], {"integer": 1, "null": 1})
        self.assertTrue(fields["x"]["nullable"])
        self.assertTrue(fields["optional"]["optional_observed"])
        self.assertIsNone(fields["x"]["required"])

    def test_arrays_schema_does_not_create_rng_choices(self):
        value = graph()
        value.observe(observation(response={"symbols": [1, None, "scatter"]}))
        self.assertEqual(len(value.data["choices"]), 1)
        field = next(iter(value.data["response_contracts"].values()))["schema"]["properties"]["symbols"]
        self.assertEqual(field["items"]["observed_types"], {"integer": 1, "null": 1, "string": 1})
        self.assertEqual(field["length"], {"min": 3, "max": 3})

    def test_array_element_optional_uses_object_count(self):
        schema = {}
        observe_schema(schema, [{"x": 1}, None, {}])
        field = schema["items"]["properties"]["x"]
        self.assertEqual(field["parent_samples"], 2)
        self.assertTrue(field["optional_observed"])

    def test_context_selected_multiplier_and_prefix(self):
        value = graph()
        value.observe(observation(prefix=("bootstrap-choice",)))
        context = next(iter(value.data["path_context"].values()))
        self.assertEqual(context["after"]["selected_multiplier"], 5)
        self.assertEqual(next(iter(value.data["decision_points"].values()))["replay_prefixes"], [["bootstrap-choice"]])

    def test_unknowns_survive_merge(self):
        value = graph()
        value.unknown("choice", "some-choice", "index domain not proven")
        value.merge(graph())
        self.assertEqual(len(value.data["unknowns"]), 1)
        self.assertFalse(value.to_dict()["coverage"]["complete"])

    def test_bounded_representative_examples(self):
        value = graph()
        for i in range(1000):
            value.observe(observation(response={"win": i}))
        self.assertEqual(len(value.data["transitions"]), 1)
        contract = next(iter(value.data["response_contracts"].values()))
        self.assertEqual(contract["observations"], 1000)
        self.assertLessEqual(len(contract["examples"]), 3)

    def test_secrets_not_persisted(self):
        value = graph()
        value.observe(observation(response={"t_key": "credential-value", "cookies": "cookie-value",
            "token": "token-value", "launch_url": "https://example.org/?secret=hidden",
            "round_series_id": 87654321, "session_uuid": "session-value"}))
        raw = json.dumps(value.to_dict())
        for secret in ("credential-value", "cookie-value", "token-value", "hidden", "87654321", "session-value", "private-round", "private-session"):
            self.assertNotIn(secret, raw)

    def test_provider_and_fingerprint_isolation(self):
        a = graph()
        for b in (StructuralMap("pragmatic", "json-flow", "fixture", "bundle-1"), graph("other"), graph(fingerprint="bundle-2")):
            with self.assertRaises(ValueError):
                a.merge(b)

    def test_persist_merges_runs_and_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analysis/game-structure.json"
            a, b = graph(), graph()
            a.observe(observation(2))
            b.observe(observation(5))
            persist_map(path, a, "run-a")
            persist_map(path, b, "run-b")
            data = persist_map(path, b, "run-b")
            self.assertEqual(data["coverage"]["executed"], 2)
            self.assertEqual(sum(c["coverage"]["attempts"] for c in data["choices"].values()), 2)

    def test_atomic_write_interruption_preserves_previous(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game-structure.json"
            atomic_write(path, {"old": True})
            with patch("tester_spin.structure.persistence.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    atomic_write(path, {"new": True})
            self.assertEqual(json.loads(path.read_text()), {"old": True})
            self.assertEqual(len(list(Path(directory).glob("*.tmp"))), 0)

    def test_concurrent_games_do_not_mix(self):
        with tempfile.TemporaryDirectory() as directory:
            def write(name):
                value = graph(name)
                value.observe(observation())
                return persist_map(Path(directory) / name / "game-structure.json", value, "run")
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(write, ["a", "b"]))
            self.assertEqual([r["metadata"]["slug"] for r in results], ["a", "b"])
            self.assertFalse(set(results[0]["states"]) & set(results[1]["states"]))

    def test_concurrent_same_game_does_not_lose_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game-structure.json"
            def write(i):
                value = graph()
                value.observe(observation(i))
                return persist_map(path, value, str(i))
            with ThreadPoolExecutor(3) as pool:
                list(pool.map(write, (2, 5, 10)))
            self.assertEqual(json.loads(path.read_text())["coverage"]["executed"], 3)

    def test_bad_version_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game-structure.json"
            atomic_write(path, {"schema": "future/v99"})
            with self.assertRaises(ValueError):
                persist_map(path, graph(), "a")
            self.assertEqual(json.loads(path.read_text())["schema"], "future/v99")

    def test_fingerprint_change_archives_old_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game-structure.json"
            a = graph()
            a.observe(observation(2))
            persist_map(path, a, "a")
            b = graph(fingerprint="bundle-2")
            b.observe(observation(5))
            data = persist_map(path, b, "b")
            revision = data["metadata"]["previous_revisions"][0]["path"]
            self.assertTrue((path.parent / revision).exists())
            self.assertEqual(data["coverage"]["choices"], 1)

    def test_explicit_structural_state_signature(self):
        value = graph()
        proof = value.evidence("CLIENT_PROVEN")
        self.assertNotEqual(value.state(State("bonus", signature="phase-a"), proof),
                            value.state(State("bonus", signature="phase-b"), proof))

    def test_pending_replay_resolves_prefix_and_filters_server_only(self):
        value = graph()
        first = value.observe(observation())
        state = State("freespins")
        value.discover(state, "cashout", [Choice("cashout", {})], prefix=(first,))
        value.discover(state, "unknown", [Choice("guess", {}, "SERVER_ADVERTISED")], prefix=(first,))
        plans = value.pending_replays()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["prefix"][0]["command"], "choose_multiplier")
        self.assertEqual(plans[0]["target"]["command"], "cashout")
        self.assertTrue(plans[0]["current_state_validation_required"])

    def test_uuid_mapping_keys_do_not_leak_and_keep_value_schema(self):
        value = graph()
        uuid = "01234567-1234-1234-1234-123456789abc"
        value.observe(observation(response={"mapping": {uuid: {"x": 1}}}))
        data = value.to_dict()
        self.assertNotIn(uuid, json.dumps(data))
        schema = next(iter(data["response_contracts"].values()))["schema"]
        self.assertIn("mapping_values", schema["properties"]["mapping"])

    def test_cross_provider_persistence_rejects_without_replacing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game-structure.json"
            persist_map(path, graph(), "a")
            with self.assertRaises(ValueError):
                persist_map(path, StructuralMap("rubyplay", "json", "fixture"), "b")
            self.assertEqual(json.loads(path.read_text())["metadata"]["provider"], "bgaming")

    def test_unsafe_revision_reference_is_rejected(self):
        data = graph().to_dict()
        data["metadata"]["previous_revisions"] = [{"path": "../../private.json"}]
        with self.assertRaises(ValueError):
            StructuralMap.load(data)


if __name__ == "__main__":
    unittest.main()
