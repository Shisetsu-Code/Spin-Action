from __future__ import annotations

import socket
import threading
import time
import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
