from __future__ import annotations

import inspect
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import requests

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.redtiger import adapter, bootstrap, catalog, execution, provider, result_tree, runtime
from tester_spin.providers.redtiger.adapter import RedTigerProvider as RedTigerAdapter
from tester_spin.providers.redtiger.provider import RedTigerProvider
from tester_spin.providers.redtiger.runtime import RedTigerRuntime


class RedTigerCatalogTests(unittest.TestCase):
    def test_studio_id_is_discovered_by_title_not_fixed_number(self) -> None:
        payload = {
            "data": [
                {"id": 17, "attributes": {"title": "Other Studio"}},
                {"id": 9137, "attributes": {"title": "Red Tiger"}},
            ]
        }
        self.assertEqual(catalog.find_studio_id(payload, "Red Tiger"), 9137)

    def test_game_identity_keeps_slug_table_id_and_game_type_independent(self) -> None:
        payload = {
            "id": 42,
            "attributes": {
                "name": "Synthetic Game",
                "slug": "synthetic-public-slug",
                "tableId": "opaque-table-7xq",
                "gameType": "DifferentRuntimeIdentity",
                "provider": "redtiger",
                "releaseDate": "2026-01-01T00:00:00.000Z",
                "hasBonusBuy": True,
                "icon": {
                    "data": {
                        "id": 99,
                        "attributes": {"url": "https://cdn.example/icon.webp"},
                    }
                },
            },
        }
        record = catalog.parse_game_record(
            payload,
            public_catalog_url="https://redtiger.example/games",
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.game.slug, "synthetic-public-slug")
        self.assertEqual(record.table_id, "opaque-table-7xq")
        self.assertEqual(record.game.symbol, "opaque-table-7xq")
        self.assertEqual(record.game_type, "DifferentRuntimeIdentity")
        self.assertEqual(
            record.game.url,
            "https://redtiger.example/games/synthetic-public-slug",
        )


class RedTigerRuntimeTests(unittest.TestCase):
    @staticmethod
    def _settings() -> dict:
        return {
            "success": True,
            "result": {
                "game": {
                    "hasFeatureBuy": True,
                    "featureBuy": [
                        {"name": "MysteryRush", "price": "37.5"},
                    ],
                    "gameModes": [],
                    "mathModes": [{"name": "IndependentMath", "id": 901}],
                },
                "user": {
                    "stakes": {
                        "types": ["0.5", "2", "5"],
                        "defaultIndex": 1,
                    }
                },
            },
        }

    def test_purchase_contract_is_derived_from_settings(self) -> None:
        settings = self._settings()
        stakes, default_stake = runtime.stakes_from_settings(settings)
        features = runtime.feature_buys_from_settings(settings)
        self.assertEqual(default_stake, Decimal("2"))
        self.assertEqual(features[0].name, "MysteryRush")
        self.assertEqual(features[0].multiplier, Decimal("37.5"))

        rt = RedTigerRuntime(
            session=requests.Session(),
            settings_url="https://g.example/x/platform/game/settings",
            spin_url="https://g.example/x/platform/game/spin",
            launcher_url="https://g.example/x/launcher/OpaqueGame",
            game_id="OpaqueRuntimeGame",
            session_id="fresh-session",
            token="fresh-token",
            user_data={"userId": 123, "fingerprint": "fresh-fingerprint"},
            custom={"siteId": "site", "context": {"ne_evo_token": "fresh-jwt"}},
            settings_request={"playMode": "demo", "listenToFrontend": True},
            settings_response=settings,
            stakes=stakes,
            default_stake=default_stake,
            currency_decimals=2,
            feature_buys=features,
        )
        payload = runtime.build_spin_payload(
            rt,
            stake=default_stake,
            feature_buy=features[0],
        )
        self.assertEqual(payload["gameId"], "OpaqueRuntimeGame")
        self.assertEqual(payload["stake"], 2)
        self.assertEqual(
            payload["extras"],
            {
                "features": {
                    "featureBuy": "MysteryRush",
                    "featureBuyCost": "75.00",
                }
            },
        )

    def test_result_tree_discovers_nested_modes_without_fixed_paths(self) -> None:
        game = {
            "spinMode": "Normal",
            "opaqueContainer": {
                "children": [
                    {
                        "spinMode": "Respin",
                        "anotherUnknownLayer": {
                            "items": [{"spinMode": "FreeSpins", "features": [{"x": 1}]}]
                        },
                    }
                ]
            },
        }
        self.assertEqual(
            result_tree.observed_modes(game),
            ["Normal", "Respin", "FreeSpins"],
        )
        nodes = result_tree.result_nodes(game)
        self.assertTrue(any("anotherUnknownLayer" in node.path for node in nodes))

    def test_public_provider_preserves_table_id_not_runtime_game_id_in_storage_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            redtiger = RedTigerProvider(Path(temp))
            game = Game(
                provider="redtiger",
                slug="synthetic-public-slug",
                name="Synthetic Game",
                url="https://redtiger.example/games/synthetic-public-slug",
                symbol="opaque-table-7xq",
            )
            inner = GameTestResult(
                provider="redtiger",
                slug=game.slug,
                game_name=game.name,
                game_url=game.url,
                requested_spins=1,
                successful_spins=1,
                failed_spins=0,
                status="OK",
                symbol="OpaqueRuntimeGame",
            )
            with patch.object(RedTigerAdapter, "test_game", return_value=inner):
                result = redtiger.test_game(
                    game,
                    spins=1,
                    timeout_s=5.0,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

            self.assertEqual(result.symbol, "opaque-table-7xq")
            self.assertEqual(game.symbol, "opaque-table-7xq")

    def test_production_module_has_no_title_specific_or_foreign_provider_contract(self) -> None:
        modules = (adapter, bootstrap, catalog, execution, provider, result_tree, runtime)
        source = "\n".join(inspect.getsource(module) for module in modules)
        self.assertNotIn("Zillard", source)
        for foreign in (
            "providers.pragmatic",
            "providers.bgaming",
            "providers.rubyplay",
            "providers.belatra",
            "providers.one_spin4win",
        ):
            self.assertNotIn(foreign, source)


if __name__ == "__main__":
    unittest.main()
