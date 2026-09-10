from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tester_spin.models import Game
from tester_spin.providers.bgaming import BGamingProvider
from tester_spin.providers.bgaming.har_capture import (
    _is_bgaming_state_post,
    _request_payload_shape,
    append_har_debug,
    ensure_analysis_har,
    find_existing_har,
)


class BGamingHARCaptureTests(unittest.TestCase):
    def test_existing_manual_har_is_reused_without_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp) / "game"
            manual = game_dir / "manual" / "captura.har"
            manual.parent.mkdir(parents=True)
            manual.write_text('{"log":{"entries":[]}}', encoding="utf-8")
            logs: list[str] = []

            with patch(
                "tester_spin.providers.bgaming.har_capture._capture_browser_har"
            ) as capture:
                result = ensure_analysis_har(
                    game_dir=game_dir,
                    game_name="Game",
                    launch_url="https://demo.bgaming-network.com/games/Game/FUN",
                    timeout_s=10,
                    stop_event=threading.Event(),
                    progress=logs.append,
                )

            capture.assert_not_called()
            self.assertTrue(result.skipped)
            self.assertFalse(result.captured)
            self.assertEqual(result.path, manual)
            self.assertTrue(any("captura omitida" in line for line in logs))
            debug = game_dir / "analysis" / "har-debug.jsonl"
            self.assertTrue(debug.is_file())
            self.assertIn("reuse_existing_har", debug.read_text(encoding="utf-8"))

    def test_provider_suite_hook_skips_existing_har_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = BGamingProvider(Path(temp))
            game = Game(
                provider="bgaming",
                slug="game",
                name="Game",
                url="https://bgaming.com/games/game",
            )
            game_dir = provider.game_dir(game)
            manual = game_dir / "manual.har"
            manual.write_text('{"log":{"entries":[]}}', encoding="utf-8")
            logs: list[str] = []

            with patch.object(provider, "_new_session") as new_session:
                provider.prepare_test_artifacts(
                    game,
                    timeout_s=10,
                    stop_event=threading.Event(),
                    progress=logs.append,
                )

            new_session.assert_not_called()
            self.assertTrue(any("captura omitida" in line for line in logs))
            self.assertEqual(provider.har_artifact_dir(game), game_dir)

    def test_provider_har_folder_falls_back_to_analysis_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = BGamingProvider(Path(temp))
            game = Game(
                provider="bgaming",
                slug="game",
                name="Game",
                url="https://bgaming.com/games/game",
            )
            game_dir = provider.game_dir(game)
            analysis = game_dir / "analysis"
            analysis.mkdir(parents=True)
            (analysis / "har-debug.jsonl").write_text("{}\n", encoding="utf-8")

            self.assertEqual(provider.har_artifact_dir(game), analysis)

    def test_empty_or_partial_hars_are_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp) / "game"
            analysis = game_dir / "analysis"
            analysis.mkdir(parents=True)
            (analysis / "empty.har").write_bytes(b"")
            (analysis / "old.partial.har").write_text("partial", encoding="utf-8")

            self.assertIsNone(find_existing_har(game_dir))

    def test_debug_log_sanitizes_session_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp) / "game"
            path = append_har_debug(
                game_dir,
                "network",
                url=(
                    "https://demo.bgaming-network.com/game"
                    "?play_token=ephemeral-secret&foo=1"
                ),
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("network", text)
            self.assertNotIn("ephemeral-secret", text)

    def test_hyperhive_post_counts_as_provider_state_request(self) -> None:
        class Request:
            method = "POST"
            url = "https://classic.demo.bgaming-network.com/hyperhive"

        self.assertTrue(_is_bgaming_state_post(Request()))

    def test_request_payload_shape_never_logs_payload_values(self) -> None:
        class Request:
            post_data_json = {
                "jsonrpc": "2.0",
                "method": "play",
                "params": {
                    "token": "SUPER-SECRET-TOKEN",
                    "req": {
                        "bet": 987654321,
                        "bet_type": "bet",
                        "purchased_feature": "bonus_buy",
                    },
                },
            }

        shape = _request_payload_shape(Request())
        serialized = json.dumps(shape, sort_keys=True)
        self.assertEqual(shape["method"], "play")
        self.assertEqual(
            shape["req_keys"],
            ["bet", "bet_type", "purchased_feature"],
        )
        self.assertIn("token", shape["params_keys"])
        self.assertNotIn("SUPER-SECRET-TOKEN", serialized)
        self.assertNotIn("987654321", serialized)
        self.assertNotIn("bonus_buy", serialized)

    def test_new_capture_is_promoted_and_metadata_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp) / "game"
            logs: list[str] = []

            def fake_capture(*, launch_url, target, timeout_s, stop_event):
                self.assertIn("bgaming-network.com", launch_url)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('{"log":{"entries":[{}]}}', encoding="utf-8")
                return 2

            with patch(
                "tester_spin.providers.bgaming.har_capture._capture_browser_har",
                side_effect=fake_capture,
            ):
                result = ensure_analysis_har(
                    game_dir=game_dir,
                    game_name="Game",
                    launch_url=(
                        "https://demo.bgaming-network.com/games/Game/FUN"
                        "?play_token=ephemeral-secret"
                    ),
                    timeout_s=10,
                    stop_event=threading.Event(),
                    progress=logs.append,
                )

            target = game_dir / "analysis" / "browser.har"
            metadata_path = game_dir / "analysis" / "har-capture.json"
            debug_path = game_dir / "analysis" / "har-debug.jsonl"
            self.assertTrue(result.captured)
            self.assertFalse(result.skipped)
            self.assertEqual(result.path, target)
            self.assertTrue(target.is_file())
            self.assertFalse((game_dir / "analysis" / "browser.partial.har").exists())
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["provider_post_requests"], 2)
            self.assertEqual(metadata["debug_log"], "har-debug.jsonl")
            self.assertNotIn("ephemeral-secret", metadata["launch_url"])
            self.assertTrue(debug_path.is_file())
            self.assertIn("har_promoted", debug_path.read_text(encoding="utf-8"))
            self.assertTrue(any("HAR guardado" in line for line in logs))

    def test_capture_failure_does_not_leave_reusable_partial_har(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp) / "game"

            def fake_capture(*, target, **_kwargs):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("broken", encoding="utf-8")
                raise RuntimeError("browser failed")

            with patch(
                "tester_spin.providers.bgaming.har_capture._capture_browser_har",
                side_effect=fake_capture,
            ):
                result = ensure_analysis_har(
                    game_dir=game_dir,
                    game_name="Game",
                    launch_url="https://demo.bgaming-network.com/games/Game/FUN",
                    timeout_s=10,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

            self.assertFalse(result.captured)
            self.assertIn("browser failed", result.error)
            self.assertIsNone(find_existing_har(game_dir))
            debug = game_dir / "analysis" / "har-debug.jsonl"
            self.assertTrue(debug.is_file())
            self.assertIn("ensure_capture_failed", debug.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
