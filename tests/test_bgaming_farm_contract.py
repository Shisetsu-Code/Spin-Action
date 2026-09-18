from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.bgaming.farm_contract import (
    build_bgaming_farm_contract,
    validate_bgaming_farm_contract,
)
from tester_spin.providers.bgaming.profile import API_V2, PROFILE_SCHEMA


def _game() -> Game:
    return Game(
        provider="bgaming",
        slug="aztec-magic-megaways",
        name="Aztec Magic MEGAWAYS",
        url="https://bgaming.com/games/aztec-magic-megaways/",
        symbol="AztecMagicMegaways",
    )


def _profile(*, validated: bool = True, family: str = API_V2) -> dict:
    return {
        "schema": PROFILE_SCHEMA,
        "capability_version": 2,
        "family": family,
        "confidence": 1.0,
        "evidence": ["init.api_version=2"],
        "spin_options": {"rows": 6},
        "command_options": {"freespin": {"rows": 6}},
        "request_extra_data": {"client_mode": "demo"},
        "spin_option_choices": {},
        "effective_bet_selector": "",
        "effective_bet_multipliers": {},
        "dynamic_purchased_feature": True,
        "purchase_feature_level_supported": False,
        "purchase_features": ["freespin_buy"],
        "rows_required": True,
        "line_count": 0,
        "variable_layout": True,
        "allowed_continuations": ["freespin", "respin"],
        "source": "https://cdn.bgaming-network.com/game/main.js",
        "bundle_sha256": "a" * 64,
        "discovery_diagnostics": [],
        "validated": validated,
    }


def _write_game_json(root: Path, *, profile: dict | None = None) -> None:
    payload = {
        "provider": "bgaming",
        "slug": "aztec-magic-megaways",
        "name": "Aztec Magic MEGAWAYS",
        "public_url": "https://bgaming.com/games/aztec-magic-megaways/",
        "identifier": "AztecMagicMegaways",
    }
    if profile is not None:
        payload["provider_protocol"] = profile
    (root / "game.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _result(*, status: str = "OK", purchase_evidence: str = "DEMOSTRADO") -> GameTestResult:
    purchase_fields = {
        "id": "PURCHASE_FREESPIN_BUY",
        "kind": "PURCHASE",
        "wire_command": "spin",
        "purchased_feature": "freespin_buy",
        "purchased_feature_level": None,
        "cost_multiplier": 100.0,
        "executable": True,
    }
    if purchase_evidence == "DEMOSTRADO":
        purchase_fields.update(
            evidence_level="REMOTE_EXECUTION",
            execution_state="PROVEN_TERMINAL",
            validated=True,
        )
    elif purchase_evidence == "NO_VALIDADO":
        purchase_fields.update(
            evidence_level="WIRE_CANDIDATE",
            execution_state="ATTEMPTED_UNVALIDATED",
            validated=False,
        )
    elif purchase_evidence == "CANDIDATO_WIRE":
        purchase_fields.update(
            evidence_level="WIRE_CANDIDATE",
            execution_state="NOT_ATTEMPTED",
            validated=False,
        )
    else:
        purchase_fields.update(
            evidence_level="SERVER_ADVERTISED",
            execution_state="WIRE_UNPROVEN",
            validated=False,
            executable=False,
        )

    return GameTestResult(
        provider="bgaming",
        slug="aztec-magic-megaways",
        game_name="Aztec Magic MEGAWAYS",
        game_url="https://bgaming.com/games/aztec-magic-megaways/",
        requested_spins=2,
        successful_spins=2 if status == "OK" else 1,
        failed_spins=0 if status == "OK" else 1,
        status=status,
        symbol="AztecMagicMegaways",
        discovered_modes=[
            {
                "id": "SPIN",
                "kind": "SPIN",
                "wire_command": "spin",
                "observed": True,
                "evidence_level": "REMOTE_EXECUTION",
                "execution_state": "PROVEN_TERMINAL",
                "validated": True,
            },
            purchase_fields,
            {
                "id": "FREESPIN",
                "kind": "CONTINUATION",
                "wire_command": "freespin",
                "observed": True,
            },
        ],
        finished_at="2026-09-14T06:00:00+00:00",
    )


class BGamingFarmContractTests(unittest.TestCase):
    def test_ok_demonstrated_modes_produce_ready_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_game_json(root, profile=_profile())
            contract = build_bgaming_farm_contract(_game(), _result(), root)

            self.assertTrue(contract["ready"], contract["unresolved"])
            self.assertEqual(contract["source"]["protocol_family"], API_V2)
            self.assertEqual(contract["bootstrap"]["strategy"], "bgaming-api-v2")
            self.assertEqual(
                contract["bootstrap"]["inputs"]["identifier"],
                "AztecMagicMegaways",
            )
            self.assertEqual(contract["protocol"]["profile"]["spin_options"], {"rows": 6})
            by_id = {mode["id"]: mode for mode in contract["modes"]}
            self.assertEqual(by_id["SPIN"]["evidence"], "DEMOSTRADO")
            self.assertEqual(
                by_id["PURCHASE_FREESPIN_BUY"]["evidence"],
                "DEMOSTRADO",
            )
            # A continuation observed inside a fully terminal OK run is proven by
            # that terminal path even though SpinAttempt is keyed by root mode.
            self.assertEqual(by_id["FREESPIN"]["evidence"], "DEMOSTRADO")
            self.assertEqual(validate_bgaming_farm_contract(contract), [])

    def test_complete_flow_choice_is_demonstrated_and_preserves_parent_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_game_json(root, profile=_profile())
            result = _result()
            result.discovered_modes.append(
                {
                    "id": "BGAMING_FLOW_CHOICE_PURCHASE_FREESPIN_BUY__PICK__ROOT",
                    "kind": "CHOICE_CONTINUATION",
                    "scope": "PURCHASE_FREESPIN_BUY",
                    "parent": "PURCHASE_FREESPIN_BUY",
                    "prefix": ["mode=select"],
                    "path_prefix": ["mode=select"],
                    "wire_command": "pick_cards",
                    "option_field": "mode",
                    "observed": True,
                    "executable": True,
                    "coverage_required": True,
                    "branch_signature": (
                        "BGAMING:flow-choice:PURCHASE_FREESPIN_BUY:"
                        "pick_cards:[\"mode=select\"]"
                    ),
                    "required_options": ["mode=select", "mode=auto"],
                    "covered_options": ["mode=select", "mode=auto"],
                    "required_samples": 1,
                    "sample_counts": {"mode=select": 1, "mode=auto": 1},
                    "source": "client-proven",
                }
            )

            contract = build_bgaming_farm_contract(_game(), result, root)

        self.assertTrue(contract["ready"], contract["unresolved"])
        by_id = {mode["id"]: mode for mode in contract["modes"]}
        choice = by_id[
            "BGAMING_FLOW_CHOICE_PURCHASE_FREESPIN_BUY__PICK__ROOT"
        ]
        self.assertEqual(choice["evidence"], "DEMOSTRADO")
        self.assertEqual(choice["options"]["parent"], "PURCHASE_FREESPIN_BUY")
        self.assertEqual(choice["options"]["prefix"], ["mode=select"])
        self.assertEqual(
            choice["options"]["required_options"],
            ["mode=select", "mode=auto"],
        )
        self.assertEqual(
            choice["options"]["covered_options"],
            ["mode=select", "mode=auto"],
        )

    def test_non_demonstrated_required_mode_blocks_promotion(self) -> None:
        for evidence in ("NO_VALIDADO", "CANDIDATO_WIRE", "SOLO_ANUNCIADO"):
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                _write_game_json(root, profile=_profile())
                contract = build_bgaming_farm_contract(
                    _game(),
                    _result(purchase_evidence=evidence),
                    root,
                )
                self.assertFalse(contract["ready"])
                self.assertIn(
                    f"MODE_NOT_DEMONSTRATED:PURCHASE_FREESPIN_BUY:{evidence}",
                    contract["unresolved"],
                )

    def test_partial_result_is_candidate_even_with_valid_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_game_json(root, profile=_profile())
            contract = build_bgaming_farm_contract(
                _game(),
                _result(status="PARCIAL"),
                root,
            )
            self.assertFalse(contract["ready"])
            self.assertIn("DISCOVERY_STATUS:PARCIAL", contract["unresolved"])

    def test_unvalidated_or_unknown_profile_blocks_promotion(self) -> None:
        for profile in (_profile(validated=False), _profile(family="unknown")):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                _write_game_json(root, profile=profile)
                contract = build_bgaming_farm_contract(_game(), _result(), root)
                self.assertFalse(contract["ready"])
                self.assertTrue(
                    any(
                        reason in {
                            "PROFILE_NOT_VALIDATED",
                            "PROTOCOL_FAMILY_UNKNOWN",
                        }
                        for reason in contract["unresolved"]
                    ),
                    contract["unresolved"],
                )

    def test_unknown_feature_discovered_in_ok_run_blocks_farm_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_game_json(root, profile=_profile())
            result = _result()
            result.discovered_modes.append(
                {
                    "id": "CHOOSE_CHEST",
                    "kind": "FEATURE",
                    "wire_command": "choose_chest",
                    "observed": False,
                    "validated": False,
                }
            )
            contract = build_bgaming_farm_contract(_game(), result, root)
            self.assertFalse(contract["ready"])
            self.assertTrue(
                any("CHOOSE_CHEST" in reason for reason in contract["unresolved"]),
                contract["unresolved"],
            )

    def test_runtime_secrets_in_persisted_profile_make_contract_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = _profile()
            profile["request_extra_data"]["session_token"] = "secret"
            _write_game_json(root, profile=profile)
            contract = build_bgaming_farm_contract(_game(), _result(), root)
            errors = validate_bgaming_farm_contract(contract)
            self.assertTrue(
                any(error.startswith("FORBIDDEN_RUNTIME_DATA:") for error in errors),
                errors,
            )
            self.assertFalse(contract["ready"])

    def test_tokenized_demo_url_is_not_persisted_as_bootstrap_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_game_json(root, profile=_profile())
            game = _game()
            game.url = "https://demo.bgaming-network.com/game?launch_token=secret"
            contract = build_bgaming_farm_contract(game, _result(), root)
            self.assertEqual(
                contract["bootstrap"]["inputs"]["public_game_url"],
                "https://bgaming.com/games/aztec-magic-megaways/",
            )
            self.assertNotIn("secret", json.dumps(contract))


if __name__ == "__main__":
    unittest.main()
