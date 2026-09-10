from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from tester_spin.models import Game
from tester_spin.providers.bgaming.adapter import BGamingProvider
from tester_spin.providers.bgaming.profile import API_V2, BGamingProfile
from tester_spin.providers.bgaming.runtime import BGamingRuntime


class Response:
    status_code = 200


class BGamingAdapterContractTests(unittest.TestCase):
    def test_profile_options_are_present_on_first_spin(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = BGamingProvider(Path(temp))
            game = Game(
                provider="bgaming",
                slug="generic-slot",
                name="Generic Slot",
                url="https://demo.bgaming-network.com/play/GenericSlot/FUN?server=demo",
                symbol="GenericSlot",
            )
            session = requests.Session()
            runtime = BGamingRuntime(
                session=session,
                launch_url="https://demo.bgaming-network.com/games/GenericSlot/FUN",
                api_url="https://demo.bgaming-network.com/api/GenericSlot/1/session",
                identifier="GenericSlot",
                csrf_header_name="X-CSRF-Token",
                csrf_header_value="secret",
                options={},
                round_series_id=1,
            )
            init = {
                "api_version": "2",
                "options": {
                    "default_bet": 100,
                    "available_bets": [100, 200],
                    "layout": {"reels": 5, "rows": 3},
                },
                "balance": {"wallet": 100000, "game": 0},
                "flow": {
                    "command": "init",
                    "state": "ready",
                    "available_actions": ["init", "spin"],
                },
            }
            spin = {
                "api_version": "2",
                "outcome": {
                    "screen": [["1", "2", "3"]] * 5,
                    "bet": 100,
                    "win": 0,
                },
                "balance": {"wallet": 99900, "game": 0},
                "flow": {
                    "round_id": 1,
                    "last_action_id": "1_1",
                    "command": "spin",
                    "state": "closed",
                    "available_actions": ["init", "spin"],
                },
            }
            sent_spin_options = []

            def fake_post(_runtime, command, **kwargs):
                if command == "init":
                    return Response(), {"command": "init"}, init
                options = dict(kwargs.get("options") or {})
                sent_spin_options.append(options)
                return Response(), {"command": command, "options": options}, spin

            profile = BGamingProfile(
                family=API_V2,
                confidence=1.0,
                evidence=["test-contract"],
                spin_options={"mode": "60"},
                source="bundle",
                bundle_sha256="bundle-hash",
            )

            with (
                patch.object(provider, "_new_session", return_value=session),
                patch(
                    "tester_spin.providers.bgaming.execution.bootstrap_game",
                    return_value=runtime,
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.discover_profile",
                    return_value=profile,
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.post_command",
                    side_effect=fake_post,
                ),
            ):
                result = provider.test_game(
                    game,
                    spins=1,
                    timeout_s=5,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

            self.assertEqual(result.status, "OK")
            self.assertEqual(result.successful_spins, 1)
            self.assertTrue(result.attempts[0].ok)
            self.assertEqual(len(sent_spin_options), 1)
            self.assertEqual(
                sent_spin_options[0],
                {"bet": 100, "mode": "60"},
            )

    def test_unexplained_bet_translation_cannot_be_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = BGamingProvider(Path(temp))
            game = Game(
                provider="bgaming",
                slug="generic-slot",
                name="Generic Slot",
                url="https://demo.bgaming-network.com/play/GenericSlot/FUN?server=demo",
            )
            session = requests.Session()
            runtime = BGamingRuntime(
                session=session,
                launch_url="https://demo.bgaming-network.com/games/GenericSlot/FUN",
                api_url="https://demo.bgaming-network.com/api/GenericSlot/1/session",
                identifier="GenericSlot",
                csrf_header_name="X-CSRF-Token",
                csrf_header_value="secret",
                options={},
                round_series_id=1,
            )
            init = {
                "api_version": "2",
                "options": {
                    "default_bet": 100,
                    "layout": {"reels": 5, "rows": 3},
                },
                "balance": {"wallet": 100000, "game": 0},
                "flow": {
                    "command": "init",
                    "state": "ready",
                    "available_actions": ["init", "spin"],
                },
            }
            translated = {
                "api_version": "2",
                "outcome": {
                    "screen": [["1", "2", "3"]] * 5,
                    "bet": 400,
                    "win": 0,
                },
                "balance": {"wallet": 99600, "game": 0},
                "flow": {
                    "round_id": 1,
                    "last_action_id": "1_1",
                    "command": "spin",
                    "state": "closed",
                    "available_actions": ["init", "spin"],
                },
            }

            def fake_post(_runtime, command, **kwargs):
                if command == "init":
                    return Response(), {"command": "init"}, init
                return Response(), {"command": command}, translated

            with (
                patch.object(provider, "_new_session", return_value=session),
                patch(
                    "tester_spin.providers.bgaming.execution.bootstrap_game",
                    return_value=runtime,
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.discover_profile",
                    return_value=BGamingProfile(
                        family=API_V2,
                        confidence=1.0,
                        source="init",
                    ),
                ),
                patch(
                    "tester_spin.providers.bgaming.execution.post_command",
                    side_effect=fake_post,
                ),
            ):
                result = provider.test_game(
                    game,
                    spins=1,
                    timeout_s=5,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

            self.assertEqual(result.status, "PARCIAL")
            self.assertEqual(result.successful_spins, 0)
            self.assertFalse(result.attempts[0].ok)
            self.assertIn("bet devuelta=400", result.attempts[0].warning)


if __name__ == "__main__":
    unittest.main()
