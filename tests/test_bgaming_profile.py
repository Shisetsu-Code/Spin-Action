from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from tester_spin.providers.bgaming.profile import (
    API_V2,
    HYPERHIVE,
    LEGACY_LINES,
    SWITCHABLE,
    UNKNOWN,
    BGamingProfile,
    classify_runtime,
    discover_profile,
    load_profile,
    save_profile,
)
from tester_spin.providers.bgaming.runtime import BGamingRuntime


class BGamingProfileTests(unittest.TestCase):
    @staticmethod
    def runtime(*, launch_url: str = "https://demo.example/games/Test/FUN", options=None):
        return BGamingRuntime(
            session=requests.Session(),
            launch_url=launch_url,
            api_url="https://demo.example/api/Test/1/session",
            identifier="Test",
            csrf_header_name="X-CSRF",
            csrf_header_value="secret",
            options=dict(options or {}),
            round_series_id=1,
        )

    def test_hyperhive_classification_uses_transport_signature_not_game_name(self) -> None:
        runtime = self.runtime(launch_url="https://demo.example/hyperhive")
        result = classify_runtime(runtime)
        self.assertEqual(result.family, HYPERHIVE)
        self.assertEqual(result.confidence, 1.0)

    def test_api_v2_classification_uses_init_contract(self) -> None:
        runtime = self.runtime()
        result = classify_runtime(
            runtime,
            {
                "api_version": "2",
                "options": {"default_bet": 20, "layout": {"reels": 5, "rows": 3}},
                "flow": {"command": "init", "state": "ready"},
            },
        )
        self.assertEqual(result.family, API_V2)

    def test_legacy_line_family_is_discovered_from_wire_shape(self) -> None:
        runtime = self.runtime()
        result = classify_runtime(
            runtime,
            {
                "options": {
                    "line_bets": [1, 2, 5],
                    "lines": [[0], [1], [2]],
                }
            },
        )
        self.assertEqual(result.family, LEGACY_LINES)

    def test_switchable_requires_lobby_evidence(self) -> None:
        init = {"wallet": 100000, "game": 0}
        weak = classify_runtime(self.runtime(), init)
        self.assertEqual(weak.family, UNKNOWN)

        strong = classify_runtime(
            self.runtime(options={"lobby_launch_url": "https://demo.example/lobby"}),
            init,
        )
        self.assertEqual(strong.family, SWITCHABLE)
        self.assertEqual(strong.confidence, 1.0)

    def test_profile_discovery_happens_before_first_spin_and_is_persistable(self) -> None:
        runtime = self.runtime()
        init = {
            "api_version": "2",
            "options": {"default_bet": 100, "layout": {"reels": 5, "rows": 3}},
            "flow": {"command": "init", "state": "ready"},
        }
        with patch(
            "tester_spin.providers.bgaming.profile.discover_api_v2_wire_profile",
            return_value={
                "spin_options": {"mode": "60"},
                "source": "https://cdn.example/bundle.js",
                "bundle_sha256": "abc",
            },
        ):
            profile = discover_profile(runtime, init, timeout_s=1)

        self.assertEqual(profile.family, API_V2)
        self.assertEqual(profile.spin_options, {"mode": "60"})
        self.assertEqual(profile.bundle_sha256, "abc")

        with tempfile.TemporaryDirectory() as temp:
            game_json = Path(temp) / "game.json"
            save_profile(game_json, profile)
            loaded = load_profile(game_json)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.spin_options, {"mode": "60"})

    def test_profile_persists_command_options_and_client_purchase_features(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_json = Path(temp) / "game.json"
            profile = BGamingProfile(
                family=API_V2,
                confidence=1.0,
                command_options={"freespin": {"rows": 3}},
                purchase_features=["bonus_buy"],
                validated=True,
            )
            save_profile(game_json, profile)
            loaded = load_profile(game_json)
            self.assertIsNotNone(loaded)
            self.assertEqual(
                loaded.command_options,
                {"freespin": {"rows": 3}},
            )
            self.assertEqual(
                loaded.purchase_features,
                ["bonus_buy"],
            )

    def test_validated_persisted_profile_avoids_rediscovery(self) -> None:
        runtime = self.runtime()
        init = {
            "api_version": "2",
            "options": {"default_bet": 100, "layout": {"reels": 5, "rows": 3}},
        }
        persisted = BGamingProfile(
            family=API_V2,
            confidence=1.0,
            spin_options={"mode": "60"},
            validated=True,
            source="bundle",
        )
        with patch(
            "tester_spin.providers.bgaming.profile.discover_api_v2_wire_profile",
            side_effect=AssertionError("validated profile should be reused"),
        ):
            profile = discover_profile(
                runtime,
                init,
                timeout_s=1,
                persisted=persisted,
            )
        self.assertEqual(profile.spin_options, {"mode": "60"})
        self.assertTrue(profile.validated)

    def test_dynamic_layout_never_depends_on_title_or_identifier(self) -> None:
        runtime = BGamingRuntime(
            session=requests.Session(),
            launch_url="https://demo.example/games/MegawaysInName/FUN",
            api_url="https://demo.example/api",
            identifier="TruewaysNamedButFixed",
            csrf_header_name="X-CSRF",
            csrf_header_value="secret",
            options={},
            round_series_id=1,
        )
        profile = discover_profile(
            runtime,
            {
                "api_version": "2",
                "options": {"default_bet": 20, "layout": {"reels": 5, "rows": 3}},
            },
            timeout_s=1,
            persisted=BGamingProfile(
                family=API_V2,
                confidence=1.0,
                validated=True,
            ),
        )
        self.assertFalse(profile.variable_layout)


if __name__ == "__main__":
    unittest.main()
