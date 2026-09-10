from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tester_spin.providers.bgaming.har_select import inspect_har, select_best_har


class BGamingHARSelectionTests(unittest.TestCase):
    @staticmethod
    def _entry(payload: dict) -> dict:
        return {
            "request": {
                "method": "POST",
                "url": "https://game.demo.bgaming-network.com/api",
                "postData": {
                    "mimeType": "application/json",
                    "text": json.dumps(payload),
                },
            },
            "response": {"status": 200, "content": {"text": "{}"}},
        }

    def _write(self, path: Path, entries: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"log": {"version": "1.2", "entries": entries}}),
            encoding="utf-8",
        )

    def test_manual_play_har_beats_automatic_bootstrap_browser_har(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp) / "game"
            automatic = game_dir / "analysis" / "browser.har"
            manual = game_dir / "analysis" / "manual-complete.har"

            self._write(
                automatic,
                [
                    self._entry(
                        {
                            "id": "init-id",
                            "jsonrpc": "2.0",
                            "method": "init",
                            "params": {"token": "secret"},
                        }
                    )
                ],
            )
            self._write(
                manual,
                [
                    self._entry(
                        {
                            "id": "304a9195-bfec-0777-4125-08af1f67a98d",
                            "jsonrpc": "2.0",
                            "method": "play",
                            "params": {
                                "token": "secret",
                                "req": {
                                    "bet": 100,
                                    "bet_type": "bet",
                                    "custom_req": {
                                        "isNormalBuy": False,
                                        "isSuperBuy": False,
                                        "action": "spin",
                                        "exponent": 2,
                                    },
                                },
                            },
                        }
                    )
                ],
            )

            selected = select_best_har(game_dir)
            self.assertEqual(selected, manual)
            quality = inspect_har(selected)
            self.assertIsNotNone(quality)
            self.assertEqual(quality.grade, "HAR_WITH_PLAY")
            self.assertEqual(quality.plays, 1)

    def test_purchase_har_beats_spin_only_har(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            game_dir = Path(temp) / "game"
            spin_only = game_dir / "spin.har"
            purchase = game_dir / "purchase.har"

            self._write(
                spin_only,
                [
                    self._entry(
                        {
                            "id": "spin",
                            "jsonrpc": "2.0",
                            "method": "play",
                            "params": {
                                "req": {
                                    "bet": 100,
                                    "bet_type": "bet",
                                    "custom_req": {"action": "spin"},
                                }
                            },
                        }
                    )
                ],
            )
            self._write(
                purchase,
                [
                    self._entry(
                        {
                            "id": "spin",
                            "jsonrpc": "2.0",
                            "method": "play",
                            "params": {
                                "req": {
                                    "bet": 100,
                                    "bet_type": "bet",
                                    "custom_req": {"action": "spin"},
                                }
                            },
                        }
                    ),
                    self._entry(
                        {
                            "id": "purchase",
                            "jsonrpc": "2.0",
                            "method": "play",
                            "params": {
                                "req": {
                                    "bet": 100,
                                    "bet_type": "bet",
                                    "purchased_feature": "buy_bonus",
                                    "custom_req": {
                                        "isSuperBuy": True,
                                        "action": "spin",
                                    },
                                }
                            },
                        }
                    ),
                ],
            )

            selected = select_best_har(game_dir)
            self.assertEqual(selected, purchase)
            quality = inspect_har(selected)
            self.assertEqual(quality.grade, "HAR_WITH_PURCHASE")
            self.assertGreaterEqual(quality.purchases, 1)

    def test_api_v2_spin_is_protocol_usable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "api-v2.har"
            self._write(
                path,
                [
                    self._entry(
                        {
                            "command": "spin",
                            "options": {"bet": 10, "volatility": "low"},
                        }
                    )
                ],
            )
            quality = inspect_har(path)
            self.assertEqual(quality.grade, "HAR_WITH_PLAY")
            self.assertEqual(quality.spins, 1)
            self.assertTrue(quality.protocol_usable)


if __name__ == "__main__":
    unittest.main()
