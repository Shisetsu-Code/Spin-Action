from __future__ import annotations

import unittest

from tester_spin.providers.bgaming.profile import SWITCHABLE
from tester_spin.providers.bgaming_farm_adapter import (
    _close_switchable_variant_spin_contract,
)


def _contract(*, demonstrated: bool = True) -> dict:
    evidence = "DEMOSTRADO" if demonstrated else "NO_VALIDADO"
    return {
        "ready": False,
        "source": {"protocol_family": SWITCHABLE},
        "modes": [
            {
                "id": "VARIANT_ALLLUCKYCLOVER5",
                "kind": "VARIANT",
                "required": True,
                "evidence": evidence,
                "executor": "lobby_switch+init+spin",
            },
            {
                "id": "VARIANT_ALLLUCKYCLOVER20",
                "kind": "VARIANT",
                "required": True,
                "evidence": evidence,
                "executor": "lobby_switch+init+spin",
            },
        ],
        "unresolved": ["SPIN_CONTRACT_MISSING"],
    }


class BGamingSwitchableFarmContractTests(unittest.TestCase):
    def test_proven_variants_close_synthetic_spin_blocker(self) -> None:
        contract = _close_switchable_variant_spin_contract(_contract())
        self.assertTrue(contract["ready"])
        self.assertEqual(contract["unresolved"], [])

    def test_unproven_variant_keeps_fail_closed_blocker(self) -> None:
        contract = _close_switchable_variant_spin_contract(
            _contract(demonstrated=False)
        )
        self.assertFalse(contract["ready"])
        self.assertEqual(contract["unresolved"], ["SPIN_CONTRACT_MISSING"])

    def test_other_unresolved_reason_still_blocks_readiness(self) -> None:
        contract = _contract()
        contract["unresolved"].append("MODE_NOT_DEMONSTRATED:EXTRA:NO_VALIDADO")
        contract = _close_switchable_variant_spin_contract(contract)
        self.assertFalse(contract["ready"])
        self.assertEqual(
            contract["unresolved"],
            ["MODE_NOT_DEMONSTRATED:EXTRA:NO_VALIDADO"],
        )


if __name__ == "__main__":
    unittest.main()
