from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from tester_spin.models import Game
from tester_spin.providers.belatra import BelatraProvider
from tester_spin.providers.one_spin4win import OneSpin4WinProvider


class FakeWebSocket:
    def __init__(self, incoming: list[str]) -> None:
        self.incoming = list(incoming)
        self.sent: list[str] = []
        self.closed = False

    def send(self, value: str) -> None:
        self.sent.append(value)

    def recv(self) -> str:
        if not self.incoming:
            raise TimeoutError("fake socket exhausted")
        return self.incoming.pop(0)

    def close(self) -> None:
        self.closed = True


class OneSpin4WinCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.provider = OneSpin4WinProvider(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_extract_catalog_page_matches_observed_webflow_har(self) -> None:
        html = """
        <html><body>
          <div class="item_portfolio is-gallery game-card-hover">
            <img
              alt="Lucky 1spin4win Hold And Win"
              class="image_portfolio-game"
              src="https://cdn.example/lucky.webp">
            <div class="hover_portfolio">
              <div class="wrap_portfolio-hover">
                <a class="link_portfolio-game w-inline-block"
                   href="/es/games/lucky-1spin4win-hold-and-win">
                  <div fs-list-field="name">Lucky 1spin4win Hold And Win</div>
                  <div class="hide" fs-list-field="slug">lucky-1spin4win-hold-and-win</div>
                </a>
                <a class="button is-small game w-button"
                   href="https://gs.1spin4win.com:10443/gmh5/games.html?game=Lucky1Spin4WinHoldAndWin&amp;currency=EUR&amp;config=1&amp;freeplay=true&amp;language=en&amp;exit=none">
                   demo
                </a>
              </div>
            </div>
          </div>
          <div role="navigation" class="w-pagination-wrapper">
            <a href="?ae0c3ebe_page=2"
               aria-label="Next Page"
               class="w-pagination-next button is-ghost">
               cargar más
            </a>
          </div>
        </body></html>
        """
        games, next_url = self.provider._extract_catalog_page(
            html,
            "https://www.1spin4win.com/es/games",
        )
        self.assertEqual(len(games), 1)
        game = games[0]
        self.assertEqual(game.slug, "lucky-1spin4win-hold-and-win")
        self.assertEqual(game.name, "Lucky 1spin4win Hold And Win")
        self.assertEqual(game.symbol, "Lucky1Spin4WinHoldAndWin")
        self.assertEqual(
            game.url,
            "https://gs.1spin4win.com:10443/gmh5/games.html?"
            "game=Lucky1Spin4WinHoldAndWin&currency=EUR&config=1&"
            "freeplay=true&language=en&exit=none",
        )
        self.assertEqual(game.thumbnail_url, "https://cdn.example/lucky.webp")
        self.assertEqual(
            next_url,
            "https://www.1spin4win.com/es/games?ae0c3ebe_page=2",
        )

    def test_runtime_source_matches_observed_game_har(self) -> None:
        config_js = 'this.gameURL = "wss://gs.1spin4win.com:443/games";'
        game_js = (
            'this.gameController.connect('
            '"VeryLucky1024","testuser2","debug","","01","","")'
        )
        ws_url, no_args = self.provider._parse_runtime_source(config_js)
        _no_ws, args = self.provider._parse_runtime_source(game_js)
        self.assertEqual(ws_url, "wss://gs.1spin4win.com:443/games")
        self.assertEqual(no_args, [])
        self.assertEqual(
            args,
            ["VeryLucky1024", "testuser2", "debug", "", "01", "", ""],
        )

    def test_observed_init_wire_recovers_runtime_parameters(self) -> None:
        init = self.provider._parse_observed_init_wire(
            'A/u2{"key":"","type":"0","data":",,freeplay,VeryLucky243,01,1,EUR,test"}'
        )
        self.assertEqual(
            init,
            {
                "game_name": "VeryLucky243",
                "version": "01",
                "wallet": "1",
                "currency": "EUR",
            },
        )

    def test_runtime_spec_falls_back_to_observed_socket_when_assets_omit_gameurl(self) -> None:
        game = Game(
            provider=self.provider.key,
            slug="very-lucky-243",
            name="Very Lucky 243",
            url=(
                "https://gs.1spin4win.com:10443/gmh5/verylucky243.html?"
                "currency=EUR&config=1&freeplay=true&language=en&exit=none"
            ),
            symbol="verylucky243",
        )

        class FakeResponse:
            def __init__(self, url: str, text: str) -> None:
                self.url = url
                self.text = text
                self.content = text.encode("utf-8")

            def raise_for_status(self) -> None:
                return None

        class FakeSession:
            cookies = type("Cookies", (), {"get_dict": lambda self: {}})()

            def get(self, url: str, **_kwargs):
                if url == game.url:
                    return FakeResponse(
                        game.url,
                        '<html><script src="/gmh5/verylucky243.js"></script></html>',
                    )
                if url.endswith("/gmh5/verylucky243.js"):
                    return FakeResponse(
                        url,
                        'this.gameController.connect('
                        '"VeryLucky243","testuser2","debug","","01","","")',
                    )
                raise AssertionError(f"unexpected URL: {url}")

        self.provider._worker_session = lambda: FakeSession()  # type: ignore[method-assign]
        self.provider._observe_runtime_bootstrap = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: {
                "ws_url": "wss://gs.1spin4win.com:443/games",
                "game_name": "VeryLucky243",
                "version": "01",
                "wallet": "1",
                "currency": "EUR",
                "frames": [],
                "error": "",
            }
        )

        with tempfile.TemporaryDirectory() as attempt:
            spec = self.provider._discover_runtime_spec(
                game,
                timeout_s=5.0,
                attempt_dir=Path(attempt),
            )

        self.assertEqual(spec["ws_url"], "wss://gs.1spin4win.com:443/games")
        self.assertEqual(spec["game_name"], "VeryLucky243")
        self.assertEqual(spec["version"], "01")
        self.assertTrue(spec["discovery"]["runtime_fallback_used"])
        self.assertFalse(spec["discovery"]["ws_from_static_assets"])
        self.assertTrue(spec["discovery"]["connect_from_static_assets"])

    def test_wire_messages_match_observed_client_protocol(self) -> None:
        self.assertEqual(
            self.provider._wire_message(
                "0",
                ",,freeplay,VeryLucky1024,01,1,EUR,test",
            ),
            'A/u2{"key":"","type":"0","data":",,freeplay,VeryLucky1024,01,1,EUR,test"}',
        )
        self.assertEqual(
            self.provider._wire_message("1", "5,2,0"),
            'A/u2{"key":"","type":"1","data":"5,2,0"}',
        )

    def test_direct_ws_spin_reaches_ok_on_type3_result(self) -> None:
        game = Game(
            provider=self.provider.key,
            slug="very-lucky-1024",
            name="Very Lucky 1024",
            url=(
                "https://gs.1spin4win.com:10443/gmh5/verylucky1024.html?"
                "currency=EUR&config=1&freeplay=true&language=en&exit=none"
            ),
            symbol="verylucky1024",
        )
        fake_ws = FakeWebSocket(
            [
                json.dumps({"type": 0, "id": "client-1"}),
                json.dumps(
                    {
                        "type": 1,
                        "g": 10,
                        "b": 10000,
                        "w": 0,
                        "bs": "1,2,5,10",
                        "b1": 1,
                        "b2": 100,
                        "b3": 2,
                        "l": 5,
                        "cp": "EUR",
                    }
                ),
                json.dumps(
                    {
                        "type": 3,
                        "g": 11,
                        "b": 9995,
                        "w": 0,
                        "b3": 2,
                        "l": 5,
                        "k2": "",
                        "k3": "",
                        "wm": 1,
                    }
                ),
            ]
        )

        def fake_spec(*_args, **_kwargs):
            attempt_dir = Path(_kwargs["attempt_dir"])
            spec = {
                "ws_url": "wss://gs.1spin4win.com:443/games",
                "origin": "https://gs.1spin4win.com:10443",
                "game_name": "VeryLucky1024",
                "version": "01",
                "wallet": "1",
                "currency": "EUR",
                "freeplay": True,
                "demo_url": game.url,
                "scripts_scanned": [],
            }
            (attempt_dir / "runtime-spec.json").write_text(
                json.dumps(spec),
                encoding="utf-8",
            )
            return spec

        self.provider._discover_runtime_spec = fake_spec  # type: ignore[method-assign]
        self.provider._open_websocket = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: fake_ws
        )

        result = self.provider.test_game(
            game,
            spins=1,
            timeout_s=5.0,
            stop_event=threading.Event(),
            progress=lambda _message: None,
        )

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.successful_spins, 1)
        self.assertEqual(result.failed_spins, 0)
        self.assertEqual(result.symbol, "VeryLucky1024")
        self.assertTrue(result.attempts[0].terminal)
        self.assertEqual(result.attempts[0].mode_kind, "SPIN")
        self.assertEqual(
            result.attempts[0].endpoint,
            "wss://gs.1spin4win.com:443/games",
        )
        self.assertEqual(result.attempts[0].wire_steps, 1)
        self.assertTrue(
            any(
                message
                == 'A/u2{"key":"","type":"0","data":",,freeplay,VeryLucky1024,01,1,EUR,test"}'
                for message in fake_ws.sent
            )
        )
        self.assertTrue(
            any(
                message == 'A/u2{"key":"","type":"1","data":"5,2,0"}'
                for message in fake_ws.sent
            )
        )

    def test_pns_keepalive_is_answered(self) -> None:
        frames: list[dict] = []
        ws = FakeWebSocket(["pns", json.dumps({"type": 1, "l": 5, "b3": 0})])
        payload = self.provider._recv_protocol_json(
            ws,
            deadline=10**12,
            frames=frames,
        )
        self.assertEqual(payload["type"], 1)
        self.assertIn("A/pns", ws.sent)

    def test_bonus_state_is_not_terminal(self) -> None:
        self.assertTrue(self.provider._d1_feature_active({"st": 5}))
        self.assertTrue(self.provider._d1_feature_active({"st": 12}))
        self.assertFalse(self.provider._d1_feature_active({"st": 0}))
        self.assertFalse(self.provider._d1_feature_active({}))


class BelatraCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.provider = BelatraProvider(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _game_payload() -> str:
        games = [
            {
                "id": 109,
                "title": "Princess Suki",
                "slug": "princess-suki",
                "image": {
                    "desktop": {
                        "x1": "https://imgproxy.example/princess-300.jpg",
                        "x2": "https://imgproxy.example/princess-600.jpg",
                        "webp_x1": "https://imgproxy.example/princess-300.webp",
                        "webp_x2": "https://imgproxy.example/princess-600.webp",
                    }
                },
                "category": {"id": 2, "title": "Ranura"},
            },
            {
                "id": 108,
                "title": "Yo-ho-ho 2048",
                "slug": "yo-ho-ho-2048",
                "image": {
                    "desktop": {
                        "webp_x2": "https://imgproxy.example/yo-ho-ho-600.webp",
                    }
                },
                "category": {"id": 2, "title": "Ranura"},
            },
        ]
        meta = {
            "current_page": 1,
            "from": 1,
            "last_page": 5,
            "per_page": 25,
            "to": 25,
            "total": 104,
        }
        return (
            'b:["$","Games",null,{"games":'
            + json.dumps(games, separators=(",", ":"))
            + '}]\n'
            + 'c:["$","Pagination",null,{"meta":'
            + json.dumps(meta, separators=(",", ":"))
            + '}]'
        )

    def test_extract_catalog_page_uses_next_rsc_game_objects(self) -> None:
        games, meta = self.provider._extract_catalog_page(
            self._game_payload(),
            "https://belatragames.com/es/games/category/2",
        )
        self.assertEqual([game.slug for game in games], ["princess-suki", "yo-ho-ho-2048"])
        self.assertEqual(games[0].name, "Princess Suki")
        self.assertEqual(games[0].symbol, "109")
        self.assertEqual(
            games[0].url,
            "https://belatragames.com/es/games/game/princess-suki",
        )
        self.assertEqual(
            games[0].thumbnail_url,
            "https://imgproxy.example/princess-600.webp",
        )
        self.assertEqual(meta["current_page"], 1)
        self.assertEqual(meta["last_page"], 5)
        self.assertEqual(meta["per_page"], 25)
        self.assertEqual(meta["total"], 104)

    def test_next_stream_decodes_split_next_push_chunks(self) -> None:
        first = 'c:["$","Pagination",null,{"meta":{"current_page":1,"last_page":'
        second = '5,"per_page":25,"total":104}}]\n'
        html = (
            "<html><body>"
            "<script>self.__next_f.push([1,"
            + json.dumps(first)
            + "])</script>"
            "<script>self.__next_f.push([1,"
            + json.dumps(second)
            + "])</script>"
            "</body></html>"
        )
        stream = self.provider._next_stream(html)
        meta = self.provider._pagination_meta(stream)
        self.assertEqual(meta["current_page"], 1)
        self.assertEqual(meta["last_page"], 5)
        self.assertEqual(meta["total"], 104)

    def test_page_urls_match_observed_spanish_category_pagination(self) -> None:
        self.assertEqual(
            self.provider._page_url(1),
            "https://belatragames.com/es/games/category/2",
        )
        self.assertEqual(
            self.provider._page_url(2),
            "https://belatragames.com/es/games/category/2/2",
        )
        self.assertEqual(
            self.provider._page_url(5),
            "https://belatragames.com/es/games/category/2/5",
        )

    def _game(self) -> Game:
        return Game(
            provider=self.provider.key,
            slug="just-a-bingo",
            name="Just a Bingo",
            url="https://belatragames.com/es/games/game/just-a-bingo",
            thumbnail_url="https://imgproxy.example/bingo.webp",
            symbol="77",
        )

    def test_resolve_demo_url_uses_observed_free_slot_url(self) -> None:
        game = self._game()
        html = (
            '<script>window.demo="https://free-slot.belatragames.com/play/just-a-bingo";</script>'
        )
        self.assertEqual(
            self.provider._resolve_demo_url(game, html),
            "https://free-slot.belatragames.com/play/just-a-bingo",
        )

    def test_resolve_demo_url_has_safe_slug_fallback(self) -> None:
        game = self._game()
        self.assertEqual(
            self.provider._resolve_demo_url(game, "<html></html>"),
            "https://free-slot.belatragames.com/play/just-a-bingo",
        )

    def test_successful_bootstrap_remains_partial_not_spin_ok(self) -> None:
        game = self._game()

        def fake_discovery(*_args, **_kwargs):
            return (
                "https://free-slot.belatragames.com/play/just-a-bingo",
                200,
                12.5,
                ["https://example.test/runtime.js"],
                ["https://example.test/spin"],
            )

        self.provider._discover_demo_protocol = fake_discovery  # type: ignore[method-assign]
        result = self.provider.test_game(
            game,
            spins=1,
            timeout_s=5.0,
            stop_event=threading.Event(),
            progress=lambda _message: None,
        )
        self.assertEqual(result.status, "PARCIAL")
        self.assertEqual(result.successful_spins, 0)
        self.assertEqual(result.attempts[0].status_code, 200)
        self.assertFalse(result.attempts[0].terminal)
        self.assertTrue(result.attempts[0].ok)


if __name__ == "__main__":
    unittest.main()
