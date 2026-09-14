from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.farm_contract import (
    SCHEMA,
    contains_forbidden_runtime_data,
    promote_contract_if_ready,
    validate_common_contract,
    write_contract_candidate,
)


def _contract(*, ready: bool = True) -> dict:
    return {
        "schema": SCHEMA,
        "provider": "synthetic",
        "game": {
            "slug": "game-a",
            "name": "Game A",
            "symbol": "GameA",
        },
        "ready": ready,
        "source": {
            "run": "2026-09-14T00:00:00+00:00",
            "protocol_family": "synthetic-v1",
        },
        "bootstrap": {
            "strategy": "synthetic",
            "inputs": {"public_game_url": "https://example.test/game-a"},
        },
        "modes": [
            {
                "id": "SPIN",
                "kind": "SPIN",
                "required": True,
                "evidence": "DEMOSTRADO",
            }
        ],
        "continuations": {"known": [], "unresolved": []},
        "terminal_contract": {"name": "synthetic-terminal"},
        "protocol": {"family": "synthetic-v1"},
        "unresolved": [],
    }


class FarmContractTests(unittest.TestCase):
    def test_valid_common_contract_passes(self) -> None:
        self.assertEqual(validate_common_contract(_contract()), [])

    def test_invalid_schema_is_rejected(self) -> None:
        contract = _contract()
        contract["schema"] = "wrong/v9"
        self.assertIn("INVALID_SCHEMA", validate_common_contract(contract))

    def test_unresolved_items_prevent_ready_contract(self) -> None:
        contract = _contract()
        contract["unresolved"] = ["UNKNOWN_CONTINUATION"]
        self.assertIn("UNRESOLVED_ITEMS", validate_common_contract(contract))

    def test_required_mode_must_be_demonstrated(self) -> None:
        for evidence in ("NO_VALIDADO", "CANDIDATO_WIRE", "SOLO_ANUNCIADO"):
            with self.subTest(evidence=evidence):
                contract = _contract()
                contract["modes"][0]["evidence"] = evidence
                errors = validate_common_contract(contract)
                self.assertIn(
                    "REQUIRED_MODE_NOT_DEMONSTRATED:SPIN",
                    errors,
                )

    def test_secret_like_runtime_keys_are_detected_recursively(self) -> None:
        contract = _contract()
        contract["protocol"]["nested"] = {
            "csrf_token": "secret",
            "session_id": "opaque",
            "authorization": "Bearer secret",
            "cookie": "sid=x",
            "round_id": "123",
            "launch_token": "abc",
        }
        paths = contains_forbidden_runtime_data(contract)
        for field in (
            "csrf_token",
            "session_id",
            "authorization",
            "cookie",
            "round_id",
            "launch_token",
        ):
            self.assertTrue(
                any(path.endswith("." + field) for path in paths),
                field,
            )

    def test_signed_or_tokenized_url_is_rejected(self) -> None:
        contract = _contract()
        contract["bootstrap"]["inputs"]["public_game_url"] = (
            "https://example.test/game-a?launch_token=secret"
        )
        errors = validate_common_contract(contract)
        self.assertTrue(
            any(error.startswith("FORBIDDEN_RUNTIME_DATA:") for error in errors),
            errors,
        )

    def test_partial_candidate_never_overwrites_published_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp)
            published = game_dir / "farm-contract.json"
            published.write_text(
                json.dumps({"sentinel": "keep"}),
                encoding="utf-8",
            )

            candidate = _contract(ready=False)
            candidate["unresolved"] = ["PROVIDER_CONTRACT_UNSUPPORTED"]
            path = write_contract_candidate(game_dir, candidate)
            promoted = promote_contract_if_ready(game_dir, candidate)

            self.assertEqual(
                path,
                game_dir / "analysis" / "farm-contract-candidate.json",
            )
            self.assertFalse(promoted)
            self.assertEqual(
                json.loads(published.read_text(encoding="utf-8")),
                {"sentinel": "keep"},
            )

    def test_ready_contract_is_promoted_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp)
            contract = _contract()
            write_contract_candidate(game_dir, contract)
            self.assertTrue(promote_contract_if_ready(game_dir, contract))
            published = json.loads(
                (game_dir / "farm-contract.json").read_text(encoding="utf-8")
            )
            self.assertEqual(published["schema"], SCHEMA)
            self.assertTrue(published["ready"])


if __name__ == "__main__":
    unittest.main()
