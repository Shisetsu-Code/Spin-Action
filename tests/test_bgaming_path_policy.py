from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import GameTestResult
from tester_spin.providers.bgaming_path_policy import (
    RESERVED_PURCHASE_OPTION_FIELDS,
    finalize_policy_artifacts,
    prepare_hyperhive_init_for_coverage,
    resolve_coverage_bet,
    scoped_choice_domains,
    wager_plan_from_init,
)


class BGamingPathPolicyTests(unittest.TestCase):
    def test_coverage_bet_prefers_provider_advertised_minimum(self) -> None:
        init_data = {
            "options": {
                "default_bet": 200,
                "available_bets": [200, 100, 20, 40],
            },
            "balance": {"wallet": 100000, "game": 0},
        }
        plan = wager_plan_from_init(init_data)
        self.assertEqual(plan.default_bet, 200.0)
        self.assertEqual(plan.coverage_bet, 20.0)
        self.assertEqual(plan.available_bets, [20.0, 40.0, 100.0, 200.0])
        self.assertEqual(resolve_coverage_bet(init_data), (20, "options.available_bets:min"))

    def test_hyperhive_uses_minimum_without_mutating_provider_init(self) -> None:
        init_data = {
            "result": {
                "config": {
                    "default_bet": 200,
                    "bet_limits": [20, 40, 100, 200],
                },
                "balance": 100000,
            }
        }
        adjusted, plan = prepare_hyperhive_init_for_coverage(init_data)
        self.assertEqual(plan.default_bet, 200.0)
        self.assertEqual(plan.coverage_bet, 20.0)
        self.assertEqual(plan.source, "result.config.bet_limits:min")
        self.assertEqual(adjusted["result"]["config"]["default_bet"], 20)
        self.assertEqual(init_data["result"]["config"]["default_bet"], 200)

    def test_purchase_identity_and_level_are_not_global_cartesian_dimensions(self) -> None:
        profile = {
            "spin_option_choices": {
                "mode": ["20", "40"],
                "variant": ["normal", "super"],
                "purchased_feature": ["bonus_buy", "freespin_buy"],
                "purchased_feature_level": ["0", "1"],
            }
        }

        def original(value):
            raw = value["spin_option_choices"]
            return [(name, list(values)) for name, values in sorted(raw.items())]

        domains = scoped_choice_domains(profile, original)
        self.assertEqual(
            domains,
            [
                ("mode", ["20", "40"]),
                ("variant", ["normal", "super"]),
            ],
        )
        self.assertEqual(
            RESERVED_PURCHASE_OPTION_FIELDS,
            {"purchased_feature", "purchased_feature_level"},
        )

    def test_unproven_server_purchase_is_catalogued_not_executed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_data = {
                "options": {
                    "default_bet": 200,
                    "available_bets": [20, 40, 200],
                    "feature_options": {
                        "feature_multipliers": {
                            "base_bet": 20,
                            "bonus_buy": 2000,
                            "freespin_buy": 4000,
                        }
                    },
                },
                "balance": {"wallet": 100000, "game": 0},
            }
            (root / "init-response.json").write_text(
                json.dumps(init_data),
                encoding="utf-8",
            )
            (root / "profile.json").write_text(
                json.dumps({"purchase_features": ["bonus_buy"]}),
                encoding="utf-8",
            )
            result = GameTestResult(
                provider="bgaming",
                slug="synthetic",
                game_name="Synthetic",
                game_url="https://example.invalid/game",
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="OK",
                discovered_modes=[
                    {
                        "id": "PURCHASE_BONUS_BUY",
                        "kind": "PURCHASE",
                        "observed": True,
                        "client_observed": True,
                        "executable": True,
                    }
                ],
                run_dir=str(root),
            )

            finalize_policy_artifacts(result)

            by_id = {
                str(item.get("id")): item
                for item in result.discovered_modes
                if isinstance(item, dict)
            }
            unresolved = by_id["PURCHASE_FREESPIN_BUY"]
            self.assertFalse(unresolved["executable"])
            self.assertTrue(unresolved["coverage_required"])
            self.assertEqual(unresolved["discovery_state"], "ADVERTISED_ONLY")
            self.assertEqual(result.status, "PARCIAL")
            self.assertIn("PURCHASE_FREESPIN_BUY", result.error)

            catalog = json.loads(
                (root / "wager-catalog.json").read_text(encoding="utf-8")
            )
            self.assertEqual(catalog["plans"][0]["coverage_bet"], 20.0)
            costs = {
                item["mode_id"]: item
                for item in catalog["plans"][0]["purchase_costs"]
            }
            self.assertEqual(
                costs["PURCHASE_BONUS_BUY"]["estimated_debit_at_coverage_bet"],
                2000.0,
            )

            paths = json.loads(
                (root / "path-catalog.json").read_text(encoding="utf-8")
            )
            path_ids = {item["id"] for item in paths["paths"]}
            self.assertIn("PURCHASE_BONUS_BUY", path_ids)
            self.assertIn("PURCHASE_FREESPIN_BUY", path_ids)
            self.assertEqual(paths["phase"], "coverage")


if __name__ == "__main__":
    unittest.main()
