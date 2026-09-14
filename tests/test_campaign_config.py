from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.campaign_config import CampaignConfig


class CampaignConfigTests(unittest.TestCase):
    def _write(self, payload: dict) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "campaign.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_loads_defaults_and_provider_sessions(self) -> None:
        path = self._write(
            {
                "providers": {
                    "pragmatic": {"enabled": True, "sessions": 3},
                    "bgaming": {"enabled": True, "sessions": 1},
                }
            }
        )
        config = CampaignConfig.load(path)

        self.assertEqual(config.natural_spins_per_game, 3000)
        self.assertEqual(config.natural_batch_size, 100)
        self.assertEqual(config.coverage_spins, 1)
        self.assertEqual(config.requests_per_minute, 2000)
        self.assertEqual(config.timeout_seconds, 60.0)
        self.assertEqual(config.max_pages, 0)
        self.assertTrue(config.stop_on_structural_event)
        self.assertEqual(config.providers["pragmatic"].sessions, 3)
        self.assertEqual(config.providers["bgaming"].sessions, 1)

    def test_provider_overrides_global_values(self) -> None:
        path = self._write(
            {
                "natural_spins_per_game": 3000,
                "natural_batch_size": 100,
                "coverage_spins": 1,
                "requests_per_minute": 2000,
                "timeout_seconds": 60,
                "providers": {
                    "rubyplay": {
                        "enabled": True,
                        "sessions": 2,
                        "natural_spins_per_game": 900,
                        "natural_batch_size": 50,
                        "coverage_spins": 2,
                        "requests_per_minute": 800,
                        "timeout_seconds": 90,
                    }
                },
            }
        )
        config = CampaignConfig.load(path)
        effective = config.for_provider("rubyplay")

        self.assertEqual(effective.sessions, 2)
        self.assertEqual(effective.natural_spins_per_game, 900)
        self.assertEqual(effective.natural_batch_size, 50)
        self.assertEqual(effective.coverage_spins, 2)
        self.assertEqual(effective.requests_per_minute, 800)
        self.assertEqual(effective.timeout_seconds, 90.0)

    def test_alias_one_spin4win_is_normalized(self) -> None:
        path = self._write(
            {"providers": {"one_spin4win": {"enabled": True, "sessions": 2}}}
        )
        config = CampaignConfig.load(path)
        self.assertIn("1spin4win", config.providers)
        self.assertNotIn("one_spin4win", config.providers)

    def test_session_cli_override_is_provider_specific(self) -> None:
        path = self._write(
            {
                "providers": {
                    "pragmatic": {"enabled": True, "sessions": 3},
                    "bgaming": {"enabled": True, "sessions": 1},
                }
            }
        )
        config = CampaignConfig.load(path).with_session_overrides(
            ["pragmatic=2", "bgaming=4"]
        )
        self.assertEqual(config.providers["pragmatic"].sessions, 2)
        self.assertEqual(config.providers["bgaming"].sessions, 4)

    def test_invalid_provider_key_fails(self) -> None:
        path = self._write({"providers": {"unknown": {"enabled": True, "sessions": 1}}})
        with self.assertRaisesRegex(ValueError, "unknown"):
            CampaignConfig.load(path)

    def test_invalid_numeric_limits_fail_closed(self) -> None:
        cases = [
            {"providers": {"pragmatic": {"sessions": 0}}},
            {"requests_per_minute": 0, "providers": {"pragmatic": {"sessions": 1}}},
            {"natural_spins_per_game": 0, "providers": {"pragmatic": {"sessions": 1}}},
            {"natural_batch_size": 0, "providers": {"pragmatic": {"sessions": 1}}},
            {"coverage_spins": 0, "providers": {"pragmatic": {"sessions": 1}}},
            {"timeout_seconds": 0, "providers": {"pragmatic": {"sessions": 1}}},
            {"max_pages": -1, "providers": {"pragmatic": {"sessions": 1}}},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    CampaignConfig.load(self._write(payload))


if __name__ == "__main__":
    unittest.main()
