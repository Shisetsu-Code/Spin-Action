from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts.local_replay_soak import discover_sample_catalogs, run_corpus
from tester_spin.local_replay_soak import replay_game_catalog, run_provider_replays


def _sample(sequence_id: str, *, attempt: int, validated: bool = True) -> dict:
    return {
        "attempt": attempt,
        "artifact_dir": f"SPIN/attempt-{attempt:05d}",
        "validated": validated,
        "terminal": True,
        "state_sequence_id": sequence_id,
        "observed_state_sequence": [[{"field": "$.data.next_action", "value": sequence_id}]],
        "evidence": [{"path": f"SPIN/attempt-{attempt:05d}/response.json", "sha256": str(attempt) * 64, "bytes": 10}],
    }


def _catalog(*, spin_samples: list[dict], purchase_samples: list[dict] | None = None) -> dict:
    groups = [
        {
            "id": "spin-group",
            "mode_id": "SPIN",
            "mode_kind": "SPIN",
            "validated_samples": sum(1 for row in spin_samples if row.get("validated")),
            "samples": spin_samples,
            "observed_state_sequences": [],
            "dominant_state_sequence_id": "base",
            "unclassified_wire_variants": [],
        }
    ]
    if purchase_samples is not None:
        groups.append(
            {
                "id": "purchase-group",
                "mode_id": "PURCHASE_1",
                "mode_kind": "PURCHASE",
                "validated_samples": len(purchase_samples),
                "samples": purchase_samples,
                "observed_state_sequences": [],
                "dominant_state_sequence_id": "purchase-special",
                "unclassified_wire_variants": [],
            }
        )
    return {
        "schema": "tester-spin/sample-catalog/v2",
        "provider": "rubyplay",
        "game": "demo-game",
        "scope": "observed_paths_only",
        "groups": groups,
    }


class LocalReplaySoakTests(unittest.TestCase):
    def test_stops_early_on_first_replayed_structural_variant(self) -> None:
        report = replay_game_catalog(
            _catalog(spin_samples=[_sample("base", attempt=1), _sample("base", attempt=2), _sample("rare", attempt=3)]),
            iterations=3000,
        )

        self.assertEqual(report["stop_reason"], "REPLAYED_STRUCTURAL_VARIANT")
        self.assertEqual(report["iterations_executed"], 3)
        self.assertEqual(report["event"]["sequence_id"], "rare")
        self.assertEqual(report["event"]["source_attempt"], 3)
        self.assertTrue(report["offline"])

    def test_dominant_only_corpus_consumes_exactly_3000_iterations(self) -> None:
        report = replay_game_catalog(
            _catalog(spin_samples=[_sample("base", attempt=1), _sample("base", attempt=2)]),
            iterations=3000,
        )

        self.assertEqual(report["stop_reason"], "BUDGET_EXHAUSTED_NO_CORPUS_VARIANT")
        self.assertEqual(report["iterations_executed"], 3000)
        self.assertIsNone(report["event"])

    def test_purchase_samples_do_not_stop_natural_spin_soak(self) -> None:
        report = replay_game_catalog(
            _catalog(
                spin_samples=[_sample("base", attempt=1)],
                purchase_samples=[_sample("purchase-special", attempt=9)],
            ),
            iterations=25,
        )

        self.assertEqual(report["iterations_executed"], 25)
        self.assertEqual(report["stop_reason"], "BUDGET_EXHAUSTED_NO_CORPUS_VARIANT")

    def test_no_valid_spin_corpus_is_fail_closed(self) -> None:
        report = replay_game_catalog(
            _catalog(spin_samples=[_sample("base", attempt=1, validated=False)]),
            iterations=3000,
        )

        self.assertEqual(report["stop_reason"], "NO_VALID_SPIN_CORPUS")
        self.assertEqual(report["iterations_executed"], 0)

    def test_replay_requires_no_network(self) -> None:
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            report = replay_game_catalog(
                _catalog(spin_samples=[_sample("base", attempt=1)]),
                iterations=50,
            )
        self.assertEqual(report["iterations_executed"], 50)

    def test_provider_scheduler_never_exceeds_three_games(self) -> None:
        lock = threading.Lock()
        active = 0
        peak = 0

        def fake_replay(catalog: dict, *, iterations: int) -> dict:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return {"provider": catalog["provider"], "game": catalog["game"], "iterations_executed": iterations}

        items = []
        for index in range(8):
            catalog = _catalog(spin_samples=[_sample("base", attempt=1)])
            catalog["provider"] = "rubyplay"
            catalog["game"] = f"game-{index}"
            items.append((f"game-{index}", catalog))

        rows = run_provider_replays(items, iterations=10, concurrency=99, replay_fn=fake_replay)

        self.assertEqual(len(rows), 8)
        self.assertLessEqual(peak, 3)
        self.assertGreaterEqual(peak, 2)

    def test_corpus_discovers_multiple_providers_and_writes_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "input"
            out = Path(temp) / "out"
            for provider, game in (("rubyplay", "alpha"), ("pragmatic", "beta")):
                catalog = _catalog(spin_samples=[_sample("base", attempt=1)])
                catalog["provider"] = provider
                catalog["game"] = game
                target = root / provider / game / "sample-catalog.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(catalog), encoding="utf-8")

            found = discover_sample_catalogs(root)
            summary = run_corpus(root, out, iterations=25, concurrency=3)

            self.assertEqual(len(found), 2)
            self.assertEqual(summary["games_total"], 2)
            self.assertEqual(summary["providers"], ["pragmatic", "rubyplay"])
            self.assertTrue((out / "rubyplay" / "alpha" / "replay-soak.json").is_file())
            self.assertTrue((out / "pragmatic" / "beta" / "replay-soak.json").is_file())
            self.assertTrue((out / "summary.json").is_file())

    def test_corpus_reports_malformed_catalog_instead_of_skipping_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "input"
            out = Path(temp) / "out"
            bad = root / "rubyplay" / "broken" / "sample-catalog.json"
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_text("{not-json", encoding="utf-8")

            summary = run_corpus(root, out, iterations=10, concurrency=3)

            self.assertEqual(summary["games_total"], 1)
            self.assertEqual(summary["rows"][0]["stop_reason"], "MALFORMED_SAMPLE_CATALOG")
            self.assertIn("JSONDecodeError", summary["rows"][0]["error"])

    def test_corpus_reports_duplicate_provider_game_identity_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "input"
            out = Path(temp) / "out"
            for folder in ("first", "second"):
                catalog = _catalog(spin_samples=[_sample("base", attempt=1)])
                catalog["provider"] = "rubyplay"
                catalog["game"] = "duplicate"
                target = root / folder / "sample-catalog.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(catalog), encoding="utf-8")

            summary = run_corpus(root, out, iterations=10, concurrency=3)

            reasons = [row["stop_reason"] for row in summary["rows"]]
            self.assertEqual(reasons.count("DUPLICATE_PROVIDER_GAME"), 2)


if __name__ == "__main__":
    unittest.main()
