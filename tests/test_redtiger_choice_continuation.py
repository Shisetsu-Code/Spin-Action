from __future__ import annotations

import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tester_spin.models import Game
from tester_spin.providers.redtiger import execution
from tester_spin.providers.redtiger.adapter import RedTigerProvider
from tester_spin.providers.redtiger.runtime import (
    FeatureBuy,
    RedTigerRuntime,
    build_choice_payload,
    choice_url_from_spin_url,
    pending_choice_from_response,
)


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def post(self, url: str, *, json: dict, timeout: float):
        self.calls.append((url, json))
        if not self.responses:
            raise AssertionError("unexpected extra POST")
        return FakeResponse(self.responses.pop(0))

    def close(self) -> None:
        self.closed = True


class RedTigerChoiceContinuationTests(unittest.TestCase):
    @staticmethod
    def _pending_free_spins() -> dict:
        return {
            "success": True,
            "result": {
                "transactions": {"roundId": 194700354},
                "game": {
                    "win": {"total": "1.00"},
                    "stake": "2.00",
                    "spinMode": "FreeSpins",
                    "choices": {
                        "selected": None,
                        "available": ["WildMultiplier", "WaysMultiplier", "Random"],
                    },
                    "features": ["FreeSpins", "SuperFreeSpins"],
                    "gameMode": 0,
                    "hasState": True,
                },
            },
        }

    @staticmethod
    def _terminal(mode: str) -> dict:
        return {
            "success": True,
            "result": {
                "transactions": {"roundId": 1},
                "game": {
                    "win": {"total": "0.00"},
                    "stake": "2.00",
                    "spinMode": mode,
                    "features": [],
                    "gameMode": 0,
                    "hasState": False,
                },
            },
        }

    @staticmethod
    def _choice_terminal() -> dict:
        return {
            "success": True,
            "result": {
                "transactions": {"roundId": 194700354},
                "game": {
                    "win": {"freeSpins": "54.00", "total": "54.00"},
                    "stake": "2.00",
                    "gameData": {
                        "spinMode": "Normal",
                        "freeSpins": [
                            {"spinMode": "FreeSpins", "features": []},
                        ],
                    },
                },
            },
        }

    def test_pending_choice_contract_matches_captured_shape(self) -> None:
        prompt = pending_choice_from_response(self._pending_free_spins())
        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertEqual(prompt.round_id, 194700354)
        self.assertEqual(
            prompt.available,
            ("WildMultiplier", "WaysMultiplier", "Random"),
        )
        self.assertEqual(
            choice_url_from_spin_url("https://g.example/x/platform/game/spin"),
            "https://g.example/x/platform/game/choice",
        )

        runtime = RedTigerRuntime(
            session=FakeSession([]),  # type: ignore[arg-type]
            settings_url="https://g.example/x/platform/game/settings",
            spin_url="https://g.example/x/platform/game/spin",
            launcher_url="https://g.example/x/launcher/AztecTribute",
            game_id="AztecTribute",
            session_id="session",
            token="token",
            user_data={"userId": 1},
            custom={"siteId": "site"},
            settings_request={"playMode": "demo", "listenToFrontend": True},
            settings_response={},
            stakes=(Decimal("2"),),
            default_stake=Decimal("2"),
            currency_decimals=2,
            feature_buys=(),
        )
        payload = build_choice_payload(runtime, prompt=prompt, choice="WaysMultiplier")
        self.assertEqual(payload["roundId"], 194700354)
        self.assertEqual(payload["choice"], "WaysMultiplier")
        self.assertNotIn("stake", payload)
        self.assertNotIn("extras", payload)

    def test_purchase_choice_is_resolved_before_next_purchase(self) -> None:
        responses = [
            self._terminal("Normal"),
            self._pending_free_spins(),
            self._choice_terminal(),
            self._terminal("SuperFreeSpins"),
        ]
        session = FakeSession(responses)
        settings = {
            "success": True,
            "result": {
                "game": {
                    "hasFeatureBuy": True,
                    "featureBuy": [
                        {"name": "FreeSpins", "price": 100},
                        {"name": "SuperFreeSpins", "price": 300},
                    ],
                    "gameModes": [],
                    "mathModes": [],
                },
                "user": {"stakes": {"types": ["2"], "defaultIndex": 0}},
            },
        }
        runtime = RedTigerRuntime(
            session=session,  # type: ignore[arg-type]
            settings_url="https://g.example/x/platform/game/settings",
            spin_url="https://g.example/x/platform/game/spin",
            launcher_url="https://g.example/x/launcher/AztecTribute",
            game_id="AztecTribute",
            session_id="session",
            token="token",
            user_data={"userId": 1},
            custom={"siteId": "site"},
            settings_request={"playMode": "demo", "listenToFrontend": True},
            settings_response=settings,
            stakes=(Decimal("2"),),
            default_stake=Decimal("2"),
            currency_decimals=2,
            feature_buys=(
                FeatureBuy("FreeSpins", Decimal("100")),
                FeatureBuy("SuperFreeSpins", Decimal("300")),
            ),
        )

        with tempfile.TemporaryDirectory() as temp:
            provider = RedTigerProvider(Path(temp))
            game = Game(
                provider="redtiger",
                slug="aztec-tribute",
                name="Aztec Tribute",
                url="https://games.evolution.com/slots/aztec-tribute/",
                symbol="37498",
            )
            with patch.object(execution, "bootstrap_game", return_value=runtime):
                result = provider.test_game(
                    game,
                    spins=1,
                    timeout_s=5.0,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.successful_spins, 3)
        self.assertEqual(result.failed_spins, 0)
        self.assertEqual(len(result.attempts), 3)
        self.assertEqual(result.attempts[1].wire_steps, 2)

        self.assertEqual(len(session.calls), 4)
        self.assertTrue(session.calls[0][0].endswith("/spin"))
        self.assertTrue(session.calls[1][0].endswith("/spin"))
        self.assertTrue(session.calls[2][0].endswith("/choice"))
        self.assertEqual(session.calls[2][1]["roundId"], 194700354)
        self.assertEqual(session.calls[2][1]["choice"], "WildMultiplier")
        self.assertTrue(session.calls[3][0].endswith("/spin"))
        self.assertEqual(
            session.calls[3][1]["extras"]["features"]["featureBuy"],
            "SuperFreeSpins",
        )
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
