from __future__ import annotations

import unittest
from types import SimpleNamespace

from tester_spin.models import GameTestResult


def _result(*, modes: list[dict], status: str = "OK") -> GameTestResult:
    return GameTestResult(
        provider="rubyplay",
        slug="demo",
        game_name="Demo",
        game_url="https://rubyplay.com/games/demo/",
        requested_spins=1,
        successful_spins=1,
        failed_spins=0,
        status=status,
        discovered_modes=modes,
    )


def _annotate(result: GameTestResult, *, capability, available, feature_type="") -> GameTestResult:
    try:
        from tester_spin.providers.rubyplay.action_inventory import (
            annotate_rubyplay_action_inventory,
        )
    except ImportError as exc:  # RED until the provider-specific inventory exists.
        raise AssertionError("RubyPlay action inventory annotator is missing") from exc

    runtime = SimpleNamespace(
        client_profile=SimpleNamespace(
            buy_feature_client_supported=capability,
            buy_feature_type=feature_type,
            buy_feature_multiplier=50.0 if feature_type else None,
            wager=2.0,
        ),
        init_data={"data": {"buy_feature_available": available}},
    )
    return annotate_rubyplay_action_inventory(result, runtime)


class RubyPlayActionInventoryTests(unittest.TestCase):
    def test_client_proven_absence_closes_spin_only_root_inventory(self) -> None:
        result = _result(
            modes=[
                {"id": "SPIN", "kind": "SPIN", "observed": True, "executable": True}
            ]
        )

        _annotate(result, capability=False, available=False)

        inventory = result.structural_map["action_inventory"]
        self.assertEqual(inventory["state"], "COMPLETE")
        self.assertEqual(inventory["root_actions"], ["SPIN"])
        self.assertEqual(inventory["missing_root_actions"], [])
        self.assertEqual(inventory["unexpected_root_actions"], [])

    def test_enabled_buy_feature_requires_matching_purchase_root(self) -> None:
        result = _result(
            modes=[
                {"id": "SPIN", "kind": "SPIN", "observed": True, "executable": True},
                {
                    "id": "PURCHASE_FREESPIN",
                    "kind": "PURCHASE",
                    "observed": True,
                    "executable": True,
                    "wire_command": "buy_feature",
                    "buy_feature_type": "freespin",
                },
            ]
        )

        _annotate(result, capability=True, available=True, feature_type="freespin")

        inventory = result.structural_map["action_inventory"]
        self.assertEqual(inventory["state"], "COMPLETE")
        self.assertEqual(
            inventory["root_actions"],
            ["PURCHASE_FREESPIN", "SPIN"],
        )

    def test_unresolved_client_capability_never_closes_inventory(self) -> None:
        result = _result(
            modes=[
                {"id": "SPIN", "kind": "SPIN", "observed": True, "executable": True}
            ]
        )

        _annotate(result, capability=None, available=False)

        inventory = result.structural_map["action_inventory"]
        self.assertEqual(inventory["state"], "UNKNOWN")
        self.assertIn("unresolved", inventory["reason"].lower())


if __name__ == "__main__":
    unittest.main()
