from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tester_spin.models import Game
from tester_spin.providers.belatra_farm_adapter import BelatraProvider
from tester_spin.providers.one_spin4win import OneSpin4WinProvider as BaseOneSpin4WinProvider
from tester_spin.providers.one_spin4win_farm_adapter import OneSpin4WinProvider
from tester_spin.providers.pragmatic import HttpBootstrap
from tester_spin.providers.pragmatic_farm_adapter import PragmaticProvider


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    def send(self, value: str) -> None:
        self.sent.append(value)

    def close(self) -> None:
        self.closed = True


class _FakeRequest:
    url = "https://example.invalid/game"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}


class _FakeResponse:
    status_code = 200
    text = '{"gs": {}}'
    content = b"na=s"
    headers = {"Content-Type": "application/json"}
    request = _FakeRequest()

    def json(self):
        return {"gs": {}}


class _FakeSession:
    def __init__(self) -> None:
        self.posts = 0

    def post(self, *args, **kwargs):
        self.posts += 1
        return _FakeResponse()


class ProviderWireRateLimitTests(unittest.TestCase):
    def test_pragmatic_state_post_reserves_provider_slot(self) -> None:
        provider = object.__new__(PragmaticProvider)
        slots: list[int] = []
        provider.acquire_provider_request_slot = lambda **_kwargs: slots.append(1) or True
        session = _FakeSession()
        bootstrap = HttpBootstrap(
            session=session,
            symbol="vsdemo",
            mgckey="demo",
            cver=None,
            endpoint="https://example.invalid/game",
            launch_url="https://example.invalid/launch",
            spin_template={},
            init_request_raw="",
            init_response_raw=b"",
            init_response={},
            calibration_request_raw="",
            calibration_response_raw=b"",
            calibration_response={},
        )

        with tempfile.TemporaryDirectory() as temp:
            provider._post_and_store(
                bootstrap,
                {"action": "doSpin", "symbol": "vsdemo"},
                Path(temp),
                step=0,
                label="entry",
                timeout_s=1.0,
            )

        self.assertEqual(session.posts, 1)
        self.assertEqual(len(slots), 1)

    def test_d1_reserves_slot_for_init_play_and_close(self) -> None:
        provider = object.__new__(OneSpin4WinProvider)
        ws = _FakeWS()
        slots: list[int] = []
        provider.acquire_provider_request_slot = lambda **_kwargs: slots.append(1) or True
        spec = {
            "ws_url": "wss://example.invalid/ws",
            "origin": "https://example.invalid",
            "game_name": "demo",
            "version": "1",
            "wallet": "wallet",
            "currency": "EUR",
        }
        init_payload = {"type": 1, "l": 20, "b3": 1}
        terminal_payload = {"type": 3, "st": 0, "l": 20, "b3": 1}

        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(provider, "_discover_runtime_spec", return_value=spec), \
                 mock.patch.object(BaseOneSpin4WinProvider, "_open_websocket", return_value=ws), \
                 mock.patch.object(provider, "_recv_protocol_json", side_effect=[init_payload, terminal_payload]):
                ok, terminal, *_rest = provider._execute_direct_ws_spin(
                    Game(provider="1spin4win", slug="demo", name="Demo", url="https://example.invalid"),
                    timeout_s=1.0,
                    attempt_dir=Path(temp),
                )

        self.assertTrue(ok)
        self.assertTrue(terminal)
        self.assertEqual(len(ws.sent), 3)
        self.assertEqual(len(slots), 3)

    def test_d1_feature_continuation_reserves_another_slot(self) -> None:
        provider = object.__new__(OneSpin4WinProvider)
        ws = _FakeWS()
        slots: list[int] = []
        provider.acquire_provider_request_slot = lambda **_kwargs: slots.append(1) or True
        spec = {
            "ws_url": "wss://example.invalid/ws",
            "origin": "https://example.invalid",
            "game_name": "demo",
            "version": "1",
            "wallet": "wallet",
            "currency": "EUR",
        }
        responses = [
            {"type": 1, "l": 20, "b3": 1},
            {"type": 3, "st": 5, "l": 20, "b3": 1},
            {"type": 3, "st": 0, "l": 20, "b3": 1},
        ]

        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(provider, "_discover_runtime_spec", return_value=spec), \
                 mock.patch.object(BaseOneSpin4WinProvider, "_open_websocket", return_value=ws), \
                 mock.patch.object(provider, "_recv_protocol_json", side_effect=responses):
                provider._execute_direct_ws_spin(
                    Game(provider="1spin4win", slug="demo", name="Demo", url="https://example.invalid"),
                    timeout_s=1.0,
                    attempt_dir=Path(temp),
                )

        # init + spin + continuation + close
        self.assertEqual(len(ws.sent), 4)
        self.assertEqual(len(slots), 4)

    def test_belatra_post_reserves_slot_before_state_request(self) -> None:
        provider = object.__new__(BelatraProvider)
        slots: list[int] = []
        provider.acquire_provider_request_slot = lambda **_kwargs: slots.append(1) or True
        session = _FakeSession()
        state = {
            "session": session,
            "endpoint": "https://example.invalid/game",
            "referer": "https://example.invalid/demo",
            "origin": "https://example.invalid",
            "secret": "0123456789abcdef",
            "sid": "session",
            "modification": 1,
            "uid": "_demo",
            "counter": 0,
            "history_id": None,
        }

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            provider, "_encrypt_payload", return_value="encrypted"
        ):
            provider._post_direct_game(
                state,
                {"q": "enter"},
                timeout_s=1.0,
                artifact_dir=Path(temp),
                label="enter",
            )

        self.assertEqual(session.posts, 1)
        self.assertEqual(len(slots), 1)


if __name__ == "__main__":
    unittest.main()
