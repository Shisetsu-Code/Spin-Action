from __future__ import annotations

import unittest

from tester_spin.providers.bgaming.farm_contract import _mode_proves_base_spin


class BGamingBaseSpinContractTests(unittest.TestCase):
    def test_canonical_spin_proves_base_primitive(self) -> None:
        self.assertTrue(
            _mode_proves_base_spin(
                {"kind": "SPIN", "wire_command": "spin"},
                evidence="DEMOSTRADO",
            )
        )

    def test_switchable_variant_proves_same_base_primitive(self) -> None:
        self.assertTrue(
            _mode_proves_base_spin(
                {
                    "kind": "VARIANT",
                    "wire_command": "lobby_switch+init+spin",
                },
                evidence="DEMOSTRADO",
            )
        )

    def test_purchase_transport_does_not_replace_normal_spin(self) -> None:
        self.assertFalse(
            _mode_proves_base_spin(
                {"kind": "PURCHASE", "wire_command": "spin"},
                evidence="DEMOSTRADO",
            )
        )

    def test_unproven_spin_never_satisfies_base_contract(self) -> None:
        self.assertFalse(
            _mode_proves_base_spin(
                {"kind": "SPIN", "wire_command": "spin"},
                evidence="NO_VALIDADO",
            )
        )


if __name__ == "__main__":
    unittest.main()
