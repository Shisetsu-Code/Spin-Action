from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tester_spin.providers.bgaming.har_capture import (
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

    def test_empty_or_partial_hars_are_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp) / "game"
            analysis = game_dir / "analysis"
            analysis.mkdir(parents=True)
            (analysis / "empty.har").write_bytes(b"")
            (analysis / "old.partial.har").write_text("partial", encoding="utf-8")

            self.assertIsNone(find_existing_har(game_dir))

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
            self.assertTrue(result.captured)
            self.assertFalse(result.skipped)
            self.assertEqual(result.path, target)
            self.assertTrue(target.is_file())
            self.assertFalse((game_dir / "analysis" / "browser.partial.har").exists())
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["post_requests"], 2)
            self.assertNotIn("ephemeral-secret", metadata["launch_url"])
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


if __name__ == "__main__":
    unittest.main()
