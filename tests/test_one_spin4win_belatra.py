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
            'a:["$","Navigation",null,{"games":["home","slots","bingo"]}]\n'
            + 'b:["$","Games",null,{"games":'
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

    def test_demo_slug_candidates_cover_observed_historical_aliases(self) -> None:
        icy = Game(
            provider=self.provider.key,
            slug="20-icy-fruits",
            name="20 Icy Fruits",
            url="https://belatragames.com/es/games/game/20-icy-fruits",
        )
        fruits = Game(
            provider=self.provider.key,
            slug="7-fruits",
            name="7 Fruits",
            url="https://belatragames.com/es/games/game/7-fruits",
        )
        golden = Game(
            provider=self.provider.key,
            slug="88-golden",
            name="88 Golden",
            url="https://belatragames.com/es/games/game/88-golden",
        )

        self.assertIn("icy-fruits", self.provider._demo_slug_candidates(icy))
        self.assertIn("seven-fruits", self.provider._demo_slug_candidates(fruits))
        self.assertIn("88-golden-88", self.provider._demo_slug_candidates(golden))

    def test_extract_demo_links_accepts_absolute_and_relative_play_paths(self) -> None:
        html = (
            '<a href="/play/icy-fruits">A</a>'
            '<script>window.demo="https://free-slot.belatragames.com/es/play/7-fruits";</script>'
        )
        links = self.provider._extract_demo_links(html)
        self.assertIn("https://free-slot.belatragames.com/play/icy-fruits", links)
        self.assertIn("https://free-slot.belatragames.com/es/play/7-fruits", links)

    def test_resolve_demo_response_skips_404_alias_and_selects_valid_candidate(self) -> None:
        game = Game(
            provider=self.provider.key,
            slug="20-icy-fruits",
            name="20 Icy Fruits",
            url="https://belatragames.com/es/games/game/20-icy-fruits",
            symbol="98",
        )

        class FakeResponse:
            def __init__(self, url: str, status_code: int, text: str = "<html></html>") -> None:
                self.url = url
                self.status_code = status_code
                self.text = text
                self.content = text.encode("utf-8")

            def raise_for_status(self) -> None:
                if self.status_code >= 400:
                    import requests
                    raise requests.HTTPError(f"{self.status_code}")

        class FakeSession:
            def __init__(self) -> None:
                self.seen: list[str] = []

            def get(self, url: str, **_kwargs):
                self.seen.append(url)
                if url.endswith("/play/20-icy-fruits"):
                    return FakeResponse(url, 404)
                if url.endswith("/es/play/20-icy-fruits"):
                    return FakeResponse(url, 404)
                if url.endswith("/play/icy-fruits"):
                    return FakeResponse(url, 200, "<html><script src='/game.js'></script></html>")
                return FakeResponse(url, 404)

        session = FakeSession()
        with tempfile.TemporaryDirectory() as attempt:
            response = self.provider._resolve_demo_response(
                session,  # type: ignore[arg-type]
                game,
                "<html></html>",
                timeout_s=5.0,
                attempt_dir=Path(attempt),
            )
            evidence = json.loads(
                (Path(attempt) / "demo-resolution.json").read_text(encoding="utf-8")
            )

        self.assertEqual(
            response.url,
            "https://free-slot.belatragames.com/play/icy-fruits",
        )
        self.assertEqual(
            evidence["selected_url"],
            "https://free-slot.belatragames.com/play/icy-fruits",
        )
        self.assertIn(
            "https://free-slot.belatragames.com/play/20-icy-fruits",
            session.seen,
        )
        self.assertIn(
            "https://free-slot.belatragames.com/play/icy-fruits",
            session.seen,
        )

    def test_promotion_pack_nickname_adds_demo_candidate(self) -> None:
        game = Game(
            provider=self.provider.key,
            slug="legacy-foo",
            name="Legacy Foo",
            url="https://belatragames.com/es/games/game/legacy-foo",
        )

        class FakeResponse:
            def __init__(self, url: str, status_code: int, text: str = "") -> None:
                self.url = url
                self.status_code = status_code
                self.text = text
                self.content = text.encode("utf-8")

        class FakeSession:
            def get(self, url: str, **_kwargs):
                if "/promotion-packs/legacy-foo" in url:
                    return FakeResponse(
                        url,
                        200,
                        "<html><body><div>Nickname:</div><div>legacy_internal</div></body></html>",
                    )
                if url.endswith("/play/legacy-internal"):
                    return FakeResponse(url, 200, "<html></html>")
                return FakeResponse(url, 404)

        with tempfile.TemporaryDirectory() as attempt:
            response = self.provider._resolve_demo_response(
                FakeSession(),  # type: ignore[arg-type]
                game,
                "",
                timeout_s=5.0,
                attempt_dir=Path(attempt),
            )

        self.assertEqual(
            response.url,
            "https://free-slot.belatragames.com/play/legacy-internal",
        )

    def test_extract_official_demo_url_from_current_belatra_frame(self) -> None:
        html = (
            'frame":"<iframe src=\\\"https://demo.bltr-static.com/belatra/demo?'
            'game=fortune_mummy\\\" allow=\\\"fullscreen\\\" />"'
        )
        self.assertEqual(
            self.provider._extract_official_demo_url(
                html,
                "https://belatragames.com/es/games/game/fortune-mummy",
            ),
            "https://demo.bltr-static.com/belatra/demo?game=fortune_mummy&language=es",
        )

    def test_parse_demo_config_matches_current_provider_shape(self) -> None:
        html = (
            '<script>var config = '
            '{"request_crypt":true,"sc":"synthetic","modification":148,'
            '"nickname":"fortune_mummy","user":{"sid":"session-1","userCurrency":"FUN"}};'
            'var next = 1;</script>'
        )
        config = self.provider._parse_demo_config(html)
        self.assertTrue(config["request_crypt"])
        self.assertEqual(config["sc"], "synthetic")
        self.assertEqual(config["modification"], 148)
        self.assertEqual(config["nickname"], "fortune_mummy")
        self.assertEqual(config["user"]["sid"], "session-1")

    def test_belatra_aes_ctr_matches_fixed_synthetic_vector(self) -> None:
        encoded = "AQIDBAUGBwjtK2jLglhsL2bCUI0hftyGfVMV"
        self.assertEqual(
            self.provider._decrypt_payload(encoded, "testkey"),
            '{"q":"start","c":1}',
        )

    def test_base_spin_request_uses_enter_parameters(self) -> None:
        request = self.provider._base_spin_request(
            {
                "gs": {
                    "betPerLine": 10,
                    "nlines": 10,
                    "linesAssortment": [5, 10],
                    "gdenom": 1,
                    "betAssortment": [1, 2, 5, 8, 10],
                    "vipMode": {"on": 1},
                    "dop": {"curModeID": 0},
                    "other": {"showingInMoney": 0},
                    "buyBonus": None,
                }
            }
        )
        self.assertEqual(
            request,
            {
                "q": "start",
                "betPerLine": 10,
                "nlines": 5,
                "denom": 1,
                "buyBonus": None,
                "selectId": None,
                "hideInsideInHistory": 0,
                "showingInMoney": 0,
                "vipOn": 1,
                "curModeID": 0,
            },
        )

    def test_base_spin_request_preserves_math_selector_when_present(self) -> None:
        request = self.provider._base_spin_request(
            {
                "gs": {
                    "betPerLine": 10,
                    "nlines": 20,
                    "linesAssortment": [20],
                    "gdenom": 1,
                    "vipMode": {"on": 0, "vipBetK": 1.2},
                    "dop": {"curModeID": 0},
                    "other": {"showingInMoney": 0},
                    "isMathElf": 1,
                }
            }
        )
        self.assertEqual(request["isMathElf"], 1)
        self.assertEqual(request["vipOn"], 0)

    def test_legacy_double_dialog_can_be_declined_with_finish(self) -> None:
        state = {
            "enter": {
                "gs": {
                    "betPerLine": 4,
                    "nlines": 15,
                    "linesAssortment": [15],
                    "gdenom": 1,
                    "other": {"showingInMoney": 0},
                }
            },
            "history_id": None,
        }
        calls: list[dict] = []

        def fake_post(_state, payload, **_kwargs):
            calls.append(dict(payload))
            if payload["q"] == "start":
                return {
                    "gs": {
                        "phaseCur": "basedeal",
                        "phaseNext": "toDoubleDialog",
                        "historyId": 174424354,
                        "curWin": 80,
                    }
                }
            if payload["q"] == "finish":
                return {
                    "gs": {
                        "phaseCur": "finished",
                        "phaseNext": "toIdle",
                        "historyId": 174424354,
                        "curWin": 80,
                    }
                }
            raise AssertionError(payload)

        self.provider._post_direct_game = fake_post  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as attempt:
            ok, terminal, steps, phase_cur, phase_next = self.provider._execute_direct_spin(
                state,
                timeout_s=5.0,
                attempt_dir=Path(attempt),
            )

        self.assertTrue(ok)
        self.assertTrue(terminal)
        self.assertEqual(steps, 2)
        self.assertEqual((phase_cur, phase_next), ("finished", "toIdle"))
        self.assertEqual(calls[1], {"q": "finish", "ghistId": 174424354})

    def test_direct_spin_start_finish_reaches_terminal(self) -> None:
        state = {
            "enter": {
                "gs": {
                    "betPerLine": 10,
                    "nlines": 10,
                    "linesAssortment": [5, 10],
                    "gdenom": 1,
                    "vipMode": {"on": 1},
                    "dop": {"curModeID": 0},
                    "other": {"showingInMoney": 0},
                }
            },
            "history_id": None,
        }
        calls: list[dict] = []

        def fake_post(_state, payload, **_kwargs):
            calls.append(dict(payload))
            if payload["q"] == "start":
                state["history_id"] = 174419267
                return {
                    "gs": {
                        "phaseCur": "basedeal",
                        "phaseNext": "toPaid",
                        "historyId": 174419267,
                    }
                }
            if payload["q"] == "finish":
                return {
                    "gs": {
                        "phaseCur": "finished",
                        "phaseNext": "toIdle",
                        "historyId": 174419267,
                    }
                }
            raise AssertionError(payload)

        self.provider._post_direct_game = fake_post  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as attempt:
            ok, terminal, steps, phase_cur, phase_next = self.provider._execute_direct_spin(
                state,
                timeout_s=5.0,
                attempt_dir=Path(attempt),
            )

        self.assertTrue(ok)
        self.assertTrue(terminal)
        self.assertEqual(steps, 2)
        self.assertEqual((phase_cur, phase_next), ("finished", "toIdle"))
        self.assertEqual(calls[0]["q"], "start")
        self.assertEqual(calls[1], {"q": "finish", "ghistId": 174419267})

    def test_runtime_signal_summary_counts_only_action_protocol_activity(self) -> None:
        events = [
            {
                "kind": "request",
                "phase": "bootstrap",
                "method": "POST",
                "resource_type": "xhr",
                "noise": False,
            },
            {
                "kind": "request",
                "phase": "action",
                "method": "GET",
                "resource_type": "image",
                "noise": False,
            },
            {
                "kind": "request",
                "phase": "action",
                "method": "POST",
                "resource_type": "xhr",
                "noise": False,
            },
            {
                "kind": "request",
                "phase": "action",
                "method": "POST",
                "resource_type": "fetch",
                "noise": True,
            },
            {
                "kind": "ws_frame",
                "phase": "action",
                "direction": "sent",
                "noise": False,
            },
            {
                "kind": "ws_frame",
                "phase": "action",
                "direction": "received",
                "noise": False,
            },
        ]
        summary = self.provider._runtime_signal_summary(events)
        self.assertEqual(summary["action_requests"], 2)
        self.assertEqual(summary["action_non_get"], 1)
        self.assertEqual(summary["action_xhr_fetch"], 1)
        self.assertEqual(summary["action_ws_sent"], 1)
        self.assertEqual(summary["action_ws_received"], 1)
        self.assertEqual(summary["action_signals"], 2)

    def test_runtime_noise_filters_analytics_hosts(self) -> None:
        self.assertTrue(
            self.provider._runtime_noise_url(
                "https://mc.yandex.ru/webvisor/123"
            )
        )
        self.assertTrue(
            self.provider._runtime_noise_url(
                "https://www.google-analytics.com/g/collect"
            )
        )
        self.assertFalse(
            self.provider._runtime_noise_url(
                "https://free-slot.belatragames.com/api/game"
            )
        )

    def test_direct_http_game_is_ok_when_spin_finishes_to_idle(self) -> None:
        game = self._game()
        state = {
            "nickname": "just_a_bingo",
            "endpoint": "https://demo.bltr-static.com/game",
            "enter": {"gs": {"phaseCur": "finished", "phaseNext": "toIdle"}},
        }

        self.provider._open_direct_game = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: state
        )
        self.provider._execute_direct_spin = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: (True, True, 2, "finished", "toIdle")
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
        self.assertEqual(result.symbol, "77")
        self.assertEqual(result.attempts[0].symbol, "77")
        self.assertEqual(result.attempts[0].status_code, 200)
        self.assertTrue(result.attempts[0].terminal)
        self.assertEqual(result.attempts[0].wire_steps, 2)
        self.assertEqual(result.discovered_modes[0]["transport"], "encrypted_http")


if __name__ == "__main__":
    unittest.main()
