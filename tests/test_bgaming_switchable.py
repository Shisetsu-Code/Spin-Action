from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from tester_spin.providers.bgaming.runtime import (
    BGamingRuntime,
    balance_total,
    flow_continuation_command,
    is_switchable_container_init,
)
from tester_spin.providers.bgaming.switchable import (
    discover_switchable_identifiers,
)


class BGamingSwitchableTests(unittest.TestCase):
    def _runtime(self) -> BGamingRuntime:
        return BGamingRuntime(
            session=requests.Session(),
            launch_url="https://demo.bgaming-network.com/games/AllLuckyClover/FUN",
            api_url="https://demo.bgaming-network.com/api/AllLuckyClover/1/session",
            identifier="AllLuckyClover",
            csrf_header_name="X-CSRF-Token",
            csrf_header_value="secret",
            options={
                "resources_path": "https://cdn.bgaming-network.com/html/AllLuckyClover",
                "games_loader_source": "https://cdn.bgaming-network.com/html/AllLuckyClover/loader.js",
                "game_bundle_source": "https://cdn.bgaming-network.com/html/AllLuckyClover/bundle.js",
                "lobby_launch_url": "https://demo.bgaming-network.com/lobby/FUN/session/launch",
            },
            round_series_id=123,
        )

    def test_detects_balance_only_container_init(self) -> None:
        data = {"wallet": 100000, "game": 0}
        self.assertTrue(is_switchable_container_init(data))
        self.assertEqual(balance_total(data), 100000)

    def test_discovers_all_lucky_clover_variants_from_bundle(self) -> None:
        runtime = self._runtime()
        bundle = (
            'var ks=["AllLuckyClover5","AllLuckyClover20",'
            '"AllLuckyClover40","AllLuckyClover100"];'
            'var unrelated="AllLuckyCloverWidget";'
        )
        with patch(
            "tester_spin.providers.bgaming.switchable._bundle_candidates",
            return_value=["https://example.test/bundle.js"],
        ), patch(
            "tester_spin.providers.bgaming.switchable._download_text",
            return_value=bundle,
        ):
            identifiers = discover_switchable_identifiers(
                runtime,
                timeout_s=1,
            )
        self.assertEqual(
            identifiers,
            [
                "AllLuckyClover5",
                "AllLuckyClover20",
                "AllLuckyClover40",
                "AllLuckyClover100",
            ],
        )

    def test_gamble_state_prefers_close_terminal_path(self) -> None:
        data = {
            "flow": {
                "state": "gamble",
                "available_actions": ["init", "gamble", "close"],
            }
        }
        self.assertEqual(flow_continuation_command(data), "close")


if __name__ == "__main__":
    unittest.main()
