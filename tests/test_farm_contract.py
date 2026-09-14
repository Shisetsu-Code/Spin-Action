from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from tester_spin.farm_contract import (
    SCHEMA,
    contains_forbidden_runtime_data,
    export_farm_contract,
    promote_contract_if_ready,
    validate_common_contract,
    write_contract_candidate,
)
from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import ProviderAdapter


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


class _UnsupportedProvider(ProviderAdapter):
    key = "synthetic"
    display_name = "Synthetic"
    catalog_url = "https://example.test"

    def __init__(self, root: Path) -> None:
        self.root = root

    def crawl_catalog(self, **_kwargs):
        return []

    def test_game(self, game, *, spins, timeout_s, stop_event, progress):
        raise AssertionError("not used")

    def farm_contract_dir(self, game: Game) -> Path | None:
        return self.root


class _ExplodingProvider(_UnsupportedProvider):
    def build_farm_contract(self, game: Game, result: GameTestResult) -> dict:
        raise RuntimeError("builder exploded")


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

    def test_unsupported_provider_emits_explicit_nonready_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            provider = _UnsupportedProvider(root)
            game = Game(
                provider="synthetic",
                slug="game-a",
                name="Game A",
                url="https://example.test/game-a",
                symbol="GameA",
            )
            result = GameTestResult(
                provider="synthetic",
                slug="game-a",
                game_name="Game A",
                game_url=game.url,
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="OK",
                symbol="GameA",
            )
            logs: list[str] = []

            export_farm_contract(provider, game, result, progress=logs.append)

            candidate = json.loads(
                (root / "analysis" / "farm-contract-candidate.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(candidate["ready"])
            self.assertIn("PROVIDER_CONTRACT_UNSUPPORTED", candidate["unresolved"])
            self.assertFalse((root / "farm-contract.json").exists())
            self.assertEqual(result.status, "OK")

    def test_export_failure_never_changes_protocol_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = _ExplodingProvider(Path(temp))
            game = Game(
                provider="synthetic",
                slug="game-a",
                name="Game A",
                url="https://example.test/game-a",
            )
            result = GameTestResult(
                provider="synthetic",
                slug="game-a",
                game_name="Game A",
                game_url=game.url,
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="OK",
            )
            logs: list[str] = []

            export_farm_contract(provider, game, result, progress=logs.append)

            self.assertEqual(result.status, "OK")
            self.assertTrue(any("farm contract ERROR" in line for line in logs), logs)


if __name__ == "__main__":
    unittest.main()
