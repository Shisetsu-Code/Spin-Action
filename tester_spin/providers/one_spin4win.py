from __future__ import annotations

import base64
import json
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from tester_spin.models import Game, GameTestResult, SpinAttempt, utc_now_iso
from tester_spin.providers.base import GameCallback, Progress, ProviderAdapter


_ONE_SPIN_LOCAL = threading.local()
_DEMO_HOST = "gs.1spin4win.com"


def _safe_folder(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", str(name).strip()).rstrip(" .")
    return value[:160] or "Unnamed Game"


def _slug_title(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


class OneSpin4WinProvider(ProviderAdapter):
    """1spin4win/D1 adapter.

    The official public portfolio observed in the supplied Firefox HAR is a
    Webflow CMS catalogue rendered as HTML. Pagination is exposed through the
    real Webflow next-page link (e.g. ?ae0c3ebe_page=2), so catalogue crawling
    is direct HTTP and does not click the UI.

    Game runtime remains WebSocket-oriented: the demo shell is opened only to
    observe the functional sockets/frames. No HTTP response is treated as a
    validated wager/spin result.
    """

    key = "1spin4win"
    display_name = "1spin4win (D1)"
    catalog_url = "https://www.1spin4win.com/games"

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.provider_root = data_root / "providers" / self.key
        self.provider_root.mkdir(parents=True, exist_ok=True)
        self.http = requests.Session()
        self.http.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            }
        )

    def game_dir(self, game: Game) -> Path:
        path = self.provider_root / _safe_folder(game.name)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _worker_session(self) -> requests.Session:
        session = getattr(_ONE_SPIN_LOCAL, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(dict(self.http.headers))
            try:
                session.cookies.update(self.http.cookies.get_dict())
            except Exception:
                pass
            _ONE_SPIN_LOCAL.session = session
        return session

    @staticmethod
    def _demo_symbol(demo_url: str) -> str:
        parsed = urlparse(demo_url)
        query = parse_qs(parsed.query)
        explicit = (query.get("game") or [""])[0].strip()
        if explicit:
            return explicit
        stem = Path(parsed.path).stem.strip()
        return "" if stem.casefold() == "games" else stem

    @staticmethod
    def _best_card_image(card: Tag, base_url: str) -> str:
        img = card.select_one("img.image_portfolio-game")
        if img is None:
            img = card.find("img")
        if img is None:
            return ""
        for key in ("src", "data-src", "data-lazy-src"):
            value = str(img.get(key) or "").strip()
            if value and not value.startswith("data:"):
                return urljoin(base_url, value)
        srcset = str(img.get("srcset") or img.get("data-srcset") or "")
        choices = [part.strip().split()[0] for part in srcset.split(",") if part.strip()]
        return urljoin(base_url, choices[-1]) if choices else ""

    def _extract_catalog_page(
        self,
        html: str,
        base_url: str,
    ) -> tuple[list[Game], str]:
        """Parse the exact Webflow catalogue structure observed in the D1 HAR."""
        soup = BeautifulSoup(html or "", "html.parser")
        found: dict[str, Game] = {}

        for card in soup.select("div.item_portfolio"):
            if not isinstance(card, Tag):
                continue

            detail = card.select_one("a.link_portfolio-game[href]")
            name_node = card.select_one('[fs-list-field="name"]')
            slug_node = card.select_one('[fs-list-field="slug"]')
            demo_link: Tag | None = None
            for anchor in card.find_all("a", href=True):
                href = str(anchor.get("href") or "")
                if (urlparse(urljoin(base_url, href)).hostname or "").casefold() == _DEMO_HOST:
                    demo_link = anchor
                    break

            detail_url = (
                urljoin(base_url, str(detail.get("href") or ""))
                if isinstance(detail, Tag)
                else ""
            )
            slug = (
                " ".join(slug_node.stripped_strings).strip().casefold()
                if isinstance(slug_node, Tag)
                else ""
            )
            if not slug and detail_url:
                path = urlparse(detail_url).path.rstrip("/")
                slug = path.rsplit("/", 1)[-1].casefold()
            if not slug:
                continue

            name = (
                " ".join(name_node.stripped_strings).strip()
                if isinstance(name_node, Tag)
                else ""
            )
            if not name:
                image = card.select_one("img.image_portfolio-game")
                name = str(image.get("alt") or "").strip() if isinstance(image, Tag) else ""
            if not name:
                name = _slug_title(slug)

            demo_url = (
                urljoin(base_url, str(demo_link.get("href") or ""))
                if isinstance(demo_link, Tag)
                else ""
            )
            # Tester-Spin must open the actual demo runtime for protocol discovery.
            # If a demo is unavailable, keep the detail page as a diagnostic fallback.
            launch_url = demo_url or detail_url
            symbol = self._demo_symbol(demo_url) if demo_url else ""

            found[slug] = Game(
                provider=self.key,
                slug=slug,
                name=name,
                url=launch_url,
                thumbnail_url=self._best_card_image(card, base_url),
                symbol=symbol,
            )

        next_link = soup.select_one("a.w-pagination-next[href]")
        next_url = ""
        if isinstance(next_link, Tag):
            next_url = urljoin(base_url, str(next_link.get("href") or "").strip())

        return list(found.values()), next_url

    def _persist_thumbnail(self, game: Game, progress: Progress) -> None:
        if not game.thumbnail_url:
            return
        root = self.game_dir(game)
        suffix = Path(urlparse(game.thumbnail_url).path).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            suffix = ".img"
        target = root / f"thumbnail{suffix}"
        if target.exists() and target.stat().st_size > 0:
            game.thumbnail_path = str(target)
            return
        try:
            response = self._worker_session().get(game.thumbnail_url, timeout=20.0)
            response.raise_for_status()
            target.write_bytes(response.content)
            game.thumbnail_path = str(target)
        except Exception as exc:
            progress(f"[{game.name}] miniatura: {type(exc).__name__}: {exc}")

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
    ) -> list[Game]:
        limit = int(max_pages)
        if limit <= 0:
            limit = 1000

        by_slug: dict[str, Game] = {}
        visited: set[str] = set()
        page_no = 1
        url = self.catalog_url
        raw_dir = self.provider_root / "catalog-pages"
        raw_dir.mkdir(parents=True, exist_ok=True)

        progress(
            "D1 catálogo HAR: Webflow HTTP paginado; siguiendo el href real de "
            "'cargar más' sin interacción gráfica."
        )

        while url and page_no <= limit and not stop_event.is_set():
            if url in visited:
                progress(f"D1 catálogo: ciclo de paginación detectado en {url}")
                break
            visited.add(url)

            response = self.http.get(url, timeout=30.0, allow_redirects=True)
            response.raise_for_status()
            (raw_dir / f"page-{page_no:03d}.html").write_text(
                response.text,
                encoding="utf-8",
                errors="replace",
            )

            page_games, next_url = self._extract_catalog_page(response.text, response.url)
            new_count = 0
            for game in page_games:
                if game.slug in by_slug:
                    continue
                by_slug[game.slug] = game
                new_count += 1
                self._persist_thumbnail(game, progress)
                if on_game is not None:
                    on_game(game)
                progress(
                    f"  + D1 {game.name} [{game.symbol or game.slug}] — {game.url}"
                )

            progress(
                f"D1 página {page_no}: juegos={len(page_games)}, "
                f"nuevos={new_count}, total={len(by_slug)}, "
                f"siguiente={'sí' if next_url else 'no'}"
            )

            if not page_games:
                break
            url = next_url
            page_no += 1

        games = sorted(by_slug.values(), key=lambda game: game.name.casefold())
        (self.provider_root / "catalog.json").write_text(
            json.dumps(
                [
                    {
                        "provider": game.provider,
                        "slug": game.slug,
                        "name": game.name,
                        "url": game.url,
                        "thumbnail_url": game.thumbnail_url,
                        "thumbnail_path": game.thumbnail_path,
                        "symbol": game.symbol,
                        "catalog_transport": "webflow_html",
                        "runtime_transport": "websocket",
                    }
                    for game in games
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        progress(f"Catálogo D1 terminado: {len(games)} juegos únicos.")
        return games

    @staticmethod
    def _frame_preview(payload: object, *, limit: int = 16384) -> dict[str, object]:
        if isinstance(payload, bytes):
            raw = payload[:limit]
            return {
                "kind": "binary",
                "size": len(payload),
                "base64": base64.b64encode(raw).decode("ascii"),
                "truncated": len(payload) > limit,
            }
        text = str(payload)
        return {
            "kind": "text",
            "size": len(text),
            "text": text[:limit],
            "truncated": len(text) > limit,
        }

    @staticmethod
    def _decode_ws_json(payload: object) -> Any | None:
        if isinstance(payload, bytes):
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                return None
        else:
            text = str(payload)
        text = text.strip()
        if not text:
            return None

        candidates = [text]
        starts = [index for index, char in enumerate(text[:64]) if char in "[{"]
        candidates.extend(text[index:] for index in starts if index > 0)

        seen: set[str] = set()
        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _is_webvisor_payload(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        query = value.get("query")
        if isinstance(query, dict):
            qkeys = {str(key).casefold() for key in query}
            if {"wv-type", "wv-check", "wv-hit"} & qkeys:
                return True
        if (
            str(value.get("resource") or "").casefold() in {"events", "webvisor"}
            and value.get("wstoken")
        ):
            body = value.get("body")
            if isinstance(body, list):
                return any(
                    isinstance(item, dict) and str(item.get("event") or "") == "sessionStart"
                    for item in body
                )
        return False

    @staticmethod
    def _is_noise_websocket_url(url: str) -> bool:
        host = (urlparse(url).hostname or "").casefold()
        return (
            host.endswith("yandex.ru")
            or host.endswith("yandex.net")
            or "metrika" in host
            or "webvisor" in host
            or host.endswith("google-analytics.com")
            or host.endswith("googletagmanager.com")
            or host.endswith("doubleclick.net")
        )

    @staticmethod
    def _parse_runtime_source(text: str) -> tuple[str, list[str]]:
        ws_url = ""
        match = re.search(
            r"""gameURL\s*=\s*["'](wss?://[^"']+)["']""",
            text or "",
            re.I,
        )
        if match:
            ws_url = match.group(1).strip()

        connect_args: list[str] = []
        match = re.search(
            r"""gameController\.connect\s*\(([^;]{1,500})\)""",
            text or "",
            re.I | re.S,
        )
        if match:
            connect_args = re.findall(r"""["']([^"']*)["']""", match.group(1))
        return ws_url, connect_args

    @staticmethod
    def _script_references(text: str) -> list[str]:
        refs: list[str] = []
        patterns = (
            r"""addJSFile\s*\(\s*["']([^"']+)["']\s*\)""",
            r"""script\.src\s*=\s*["']([^"']+)["']""",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, text or "", re.I):
                value = match.group(1).strip()
                if value and value not in refs:
                    refs.append(value)
        return refs

    def _discover_runtime_spec(
        self,
        game: Game,
        *,
        timeout_s: float,
        attempt_dir: Path,
    ) -> dict[str, Any]:
        session = self._worker_session()
        response = session.get(game.url, timeout=timeout_s, allow_redirects=True)
        response.raise_for_status()
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "demo-page.html").write_text(
            response.text,
            encoding="utf-8",
            errors="replace",
        )

        parsed_demo = urlparse(response.url)
        origin = f"{parsed_demo.scheme}://{parsed_demo.netloc}"
        query = parse_qs(parsed_demo.query)
        freeplay = (query.get("freeplay") or [""])[0].casefold() == "true"
        if not freeplay:
            raise RuntimeError(
                "D1 direct WS sólo está habilitado para la demo pública freeplay."
            )

        soup = BeautifulSoup(response.text or "", "html.parser")
        queue: list[str] = []
        for node in soup.find_all("script", src=True):
            src = urljoin(response.url, str(node.get("src") or ""))
            if src and src not in queue:
                queue.append(src)

        scanned: list[str] = []
        ws_url = ""
        connect_args: list[str] = []
        while queue and len(scanned) < 16:
            src = queue.pop(0)
            if src in scanned:
                continue
            scanned.append(src)
            try:
                js = session.get(src, timeout=min(timeout_s, 20.0), allow_redirects=True)
                js.raise_for_status()
                if len(js.content) > 8 * 1024 * 1024:
                    continue
                text = js.text
            except Exception:
                continue

            candidate_ws, candidate_args = self._parse_runtime_source(text)
            if candidate_ws and not ws_url:
                ws_url = candidate_ws
            if len(candidate_args) >= 7 and not connect_args:
                connect_args = candidate_args[:7]

            # These paths are inserted into the document by the loader, so they
            # resolve against the demo document URL, not against the loader file.
            for ref in self._script_references(text):
                child = urljoin(response.url, ref)
                if child not in scanned and child not in queue:
                    queue.append(child)

        if not ws_url:
            raise RuntimeError("D1: no se resolvió gameURL WebSocket desde los assets.")
        if len(connect_args) < 7:
            raise RuntimeError("D1: no se resolvió gameController.connect(...) desde el JS.")

        game_name = connect_args[0].strip()
        version = connect_args[4].strip()
        wallet = (query.get("config") or [connect_args[5]])[0].strip()
        currency = (query.get("currency") or [connect_args[6]])[0].strip()
        if not game_name or not version:
            raise RuntimeError("D1: runtime gameName/version incompletos.")
        if not currency:
            currency = "EUR"

        spec = {
            "ws_url": ws_url,
            "origin": origin,
            "game_name": game_name,
            "version": version,
            "wallet": wallet,
            "currency": currency,
            "freeplay": True,
            "demo_url": response.url,
            "scripts_scanned": scanned,
        }
        (attempt_dir / "runtime-spec.json").write_text(
            json.dumps(spec, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return spec

    def _open_websocket(self, spec: dict[str, Any], timeout_s: float):
        import websocket

        cookie = "; ".join(
            f"{name}={value}"
            for name, value in self._worker_session().cookies.get_dict().items()
        )
        return websocket.create_connection(
            str(spec["ws_url"]),
            timeout=max(1.0, float(timeout_s)),
            origin=str(spec["origin"]),
            cookie=cookie or None,
            header=[
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) "
                "Gecko/20100101 Firefox/155.0"
            ],
        )

    @staticmethod
    def _wire_message(message_type: str, data: str, *, key: str = "") -> str:
        return "A/u2" + json.dumps(
            {"key": key, "type": str(message_type), "data": data},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _int_field(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _d1_feature_active(cls, payload: dict[str, Any]) -> bool:
        state = cls._int_field(payload.get("st"), 0)
        return state in {5, 6, 11, 12}

    def _recv_protocol_json(
        self,
        ws,
        *,
        deadline: float,
        frames: list[dict[str, Any]],
    ) -> dict[str, Any]:
        while time.monotonic() < deadline:
            raw = ws.recv()
            frames.append(
                {
                    "direction": "received",
                    "payload": self._frame_preview(raw),
                }
            )
            if raw == "pns":
                pong = "A/pns"
                ws.send(pong)
                frames.append(
                    {
                        "direction": "sent",
                        "payload": self._frame_preview(pong),
                        "classification": "keepalive",
                    }
                )
                continue
            decoded = self._decode_ws_json(raw)
            if isinstance(decoded, dict):
                return decoded
        raise TimeoutError("D1: timeout esperando frame JSON del servidor.")

    def _execute_direct_ws_spin(
        self,
        game: Game,
        *,
        timeout_s: float,
        attempt_dir: Path,
        wire_guard: int = 128,
    ) -> tuple[bool, bool, float, str, list[dict[str, Any]], str]:
        started = time.monotonic()
        spec = self._discover_runtime_spec(
            game,
            timeout_s=timeout_s,
            attempt_dir=attempt_dir,
        )
        frames: list[dict[str, Any]] = []
        warning = ""
        ws = self._open_websocket(spec, timeout_s)
        try:
            init_data = (
                f",,freeplay,{spec['game_name']},{spec['version']},"
                f"{spec['wallet']},{spec['currency']},test"
            )
            init_wire = self._wire_message("0", init_data)
            ws.send(init_wire)
            frames.append(
                {
                    "direction": "sent",
                    "classification": "init",
                    "payload": self._frame_preview(init_wire),
                }
            )

            deadline = time.monotonic() + max(2.0, timeout_s)
            init_payload: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                payload = self._recv_protocol_json(
                    ws,
                    deadline=deadline,
                    frames=frames,
                )
                message_type = self._int_field(payload.get("type"), -1)
                if message_type == 2:
                    raise RuntimeError(
                        "D1 init error: "
                        + str(payload.get("error") or payload.get("errorCode") or payload)
                    )
                if message_type == 1:
                    init_payload = payload
                    break

            if init_payload is None:
                raise TimeoutError("D1: no llegó respuesta type=1 de inicialización.")

            lines = self._int_field(init_payload.get("l"), 0)
            bet_index = self._int_field(init_payload.get("b3"), -1)
            if lines <= 0 or bet_index < 0:
                raise RuntimeError(
                    f"D1 init incompleto: l={init_payload.get('l')!r}, "
                    f"b3={init_payload.get('b3')!r}"
                )

            result_payload: dict[str, Any] | None = None
            terminal = False
            steps = 0
            while steps < wire_guard:
                steps += 1
                play_data = f"{lines},{bet_index},0"
                play_wire = self._wire_message("1", play_data)
                ws.send(play_wire)
                frames.append(
                    {
                        "direction": "sent",
                        "classification": "spin" if steps == 1 else "continuation",
                        "payload": self._frame_preview(play_wire),
                    }
                )

                result_deadline = time.monotonic() + max(2.0, timeout_s)
                while time.monotonic() < result_deadline:
                    payload = self._recv_protocol_json(
                        ws,
                        deadline=result_deadline,
                        frames=frames,
                    )
                    message_type = self._int_field(payload.get("type"), -1)
                    if message_type == 2:
                        raise RuntimeError(
                            "D1 spin error: "
                            + str(payload.get("error") or payload.get("errorCode") or payload)
                        )
                    if message_type == 3:
                        result_payload = payload
                        break
                if result_payload is None:
                    raise TimeoutError("D1: no llegó resultado type=3 de la tirada.")

                if not self._d1_feature_active(result_payload):
                    terminal = True
                    break

                # The official client continues bonus/free-spin states through the
                # same playGame() -> type=1 path. Reuse lines/betIndex and guard
                # against malformed or endless feature state.
                bet_index = self._int_field(result_payload.get("b3"), bet_index)
                lines = self._int_field(result_payload.get("l"), lines)
                result_payload = None

            if not terminal:
                warning = f"D1: límite de {wire_guard} continuaciones WS alcanzado."

            elapsed_ms = (time.monotonic() - started) * 1000.0
            artifact = {
                "spec": spec,
                "terminal": terminal,
                "wire_steps": steps,
                "warning": warning,
                "frames": frames,
                "final_result": result_payload,
            }
            (attempt_dir / "ws-attempt.json").write_text(
                json.dumps(artifact, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True, terminal, elapsed_ms, str(spec["ws_url"]), frames, warning
        finally:
            try:
                close_wire = self._wire_message("3", "close")
                ws.send(close_wire)
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass

    def _observe_game_websockets(
        self,
        entry_url: str,
        *,
        timeout_s: float,
        attempt_dir: Path,
    ) -> tuple[list[str], list[dict[str, object]], str]:
        sockets: list[str] = []
        frames: list[dict[str, object]] = []
        error = ""
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()

                def on_websocket(ws) -> None:
                    url = str(ws.url)
                    noise_url = self._is_noise_websocket_url(url)
                    if not noise_url and url not in sockets:
                        sockets.append(url)

                    def capture(direction: str):
                        def handler(payload) -> None:
                            if len(frames) >= 300:
                                return
                            decoded = self._decode_ws_json(payload)
                            telemetry = noise_url or self._is_webvisor_payload(decoded)
                            frames.append(
                                {
                                    "direction": direction,
                                    "websocket_url": url,
                                    "classification": (
                                        "telemetry_ignored"
                                        if telemetry
                                        else "provider_or_unknown"
                                    ),
                                    "payload": self._frame_preview(payload),
                                }
                            )
                        return handler

                    ws.on("framesent", capture("sent"))
                    ws.on("framereceived", capture("received"))

                page.on("websocket", on_websocket)
                page.goto(
                    entry_url,
                    wait_until="domcontentloaded",
                    timeout=max(1_000, int(timeout_s * 1000)),
                )
                page.wait_for_timeout(int(min(max(timeout_s, 2.0), 10.0) * 1000))
                context.close()
                browser.close()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        (attempt_dir / "runtime-websocket.json").write_text(
            json.dumps(
                {
                    "entry_url": entry_url,
                    "transport": "websocket",
                    "websocket_urls": sockets,
                    "frames": frames,
                    "error": error,
                    "note": (
                        "Passive runtime observation only. The public catalogue is "
                        "Webflow HTML, while game protocol discovery remains WebSocket."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return sockets, frames, error

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        repetitions = max(1, int(spins))
        started_iso = utc_now_iso()
        started = time.monotonic()
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        run_dir = self.game_dir(game) / "tests" / f"{stamp}-1spin4win-direct-ws"
        attempts: list[SpinAttempt] = []
        successes = 0
        responded = 0
        errors: list[str] = []
        resolved_symbol = game.symbol

        progress(
            f"[{game.name}] D1: ejecutando {repetitions} tirada(s) directamente "
            "contra el WebSocket del cliente oficial."
        )

        for number in range(1, repetitions + 1):
            if stop_event.is_set():
                break
            attempt_dir = run_dir / f"attempt-{number:03d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            try:
                ok, terminal, elapsed_ms, endpoint, frames, warning = (
                    self._execute_direct_ws_spin(
                        game,
                        timeout_s=timeout_s,
                        attempt_dir=attempt_dir,
                    )
                )
                responded += int(ok)
                successes += int(ok and terminal)

                try:
                    spec = json.loads(
                        (attempt_dir / "runtime-spec.json").read_text(encoding="utf-8")
                    )
                    resolved_symbol = str(spec.get("game_name") or resolved_symbol)
                except Exception:
                    pass

                attempts.append(
                    SpinAttempt(
                        number=number,
                        ok=ok,
                        mode_id="SPIN",
                        mode_kind="SPIN",
                        elapsed_ms=elapsed_ms,
                        symbol=resolved_symbol,
                        endpoint=endpoint,
                        terminal=terminal,
                        wire_steps=sum(
                            1
                            for frame in frames
                            if frame.get("direction") == "sent"
                            and frame.get("classification") in {"spin", "continuation"}
                        ),
                        warning=warning,
                        artifact_dir=str(attempt_dir),
                    )
                )
                progress(
                    f"[{game.name}] SPIN {number}/{repetitions}: "
                    f"{'OK' if terminal else 'PARCIAL'} {elapsed_ms:.0f} ms, "
                    f"frames={len(frames)}"
                    + (f"; {warning}" if warning else "")
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                errors.append(message)
                attempts.append(
                    SpinAttempt(
                        number=number,
                        ok=False,
                        mode_id="SPIN",
                        mode_kind="SPIN",
                        symbol=resolved_symbol,
                        terminal=False,
                        error=message,
                        artifact_dir=str(attempt_dir),
                    )
                )
                progress(f"[{game.name}] SPIN {number}/{repetitions}: ERROR {message}")

        elapsed_total = (time.monotonic() - started) * 1000.0
        attempted = len(attempts)
        if attempted and successes == attempted:
            status = "OK"
            error = ""
        elif responded:
            status = "PARCIAL"
            error = (
                f"D1 respondió {responded}/{attempted}; "
                f"tiradas terminales={successes}/{attempted}."
            )
        else:
            status = "ERROR"
            error = errors[0] if errors else "No se completó ninguna tirada D1."

        result = GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=repetitions,
            successful_spins=successes,
            failed_spins=sum(1 for attempt in attempts if not attempt.ok),
            status=status,
            symbol=resolved_symbol,
            discovered_modes=[
                {
                    "id": "SPIN",
                    "kind": "SPIN",
                    "transport": "websocket",
                    "catalog_transport": "webflow_html",
                    "automated": True,
                    "spin_validated": successes > 0,
                    "wire_prefix": "A/u2",
                    "init_type": "0",
                    "spin_type": "1",
                    "result_type": 3,
                }
            ],
            started_at=started_iso,
            finished_at=utc_now_iso(),
            elapsed_ms=elapsed_total,
            error=error,
            run_dir=str(run_dir),
            attempts=attempts,
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "result.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result
