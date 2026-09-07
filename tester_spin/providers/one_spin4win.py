from __future__ import annotations

import base64
import json
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from tester_spin.models import Game, GameTestResult, SpinAttempt, utc_now_iso
from tester_spin.providers.base import GameCallback, Progress, ProviderAdapter


_ONE_SPIN_LOCAL = threading.local()

_GAME_CONTEXT_KEYS = {
    "games",
    "game",
    "catalog",
    "catalogue",
    "portfolio",
    "items",
    "titles",
    "slots",
    "content",
}
_NAME_KEYS = ("gameName", "game_name", "name", "title", "displayName", "display_name")
_ID_KEYS = ("gameId", "game_id", "gameID", "symbol", "code", "identifier", "slug")
_URL_KEYS = (
    "launchUrl",
    "launch_url",
    "gameUrl",
    "game_url",
    "demoUrl",
    "demo_url",
    "playUrl",
    "play_url",
    "url",
    "href",
)
_IMAGE_KEYS = (
    "thumbnailUrl",
    "thumbnail_url",
    "thumbnail",
    "imageUrl",
    "image_url",
    "image",
    "iconUrl",
    "icon_url",
    "icon",
    "preview",
)


def _safe_folder(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", str(name).strip()).rstrip(" .")
    return value[:160] or "Unnamed Game"


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")
    return normalized[:180] or "unknown-game"


def _slug_title(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


def _first_scalar(value: Any) -> str:
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    if isinstance(value, dict):
        for key in ("url", "src", "href", "value", "desktop", "mobile", "default"):
            if key in value:
                candidate = _first_scalar(value[key])
                if candidate:
                    return candidate
    if isinstance(value, list):
        for item in value:
            candidate = _first_scalar(item)
            if candidate:
                return candidate
    return ""


def _pick(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in data:
            value = _first_scalar(data[key])
            if value:
                return value
    return ""


class OneSpin4WinProvider(ProviderAdapter):
    """1spin4win/D1 adapter.

    D1 provider data is treated as WebSocket-native: catalogue, session/game
    state and future wager actions are discovered from WS frames. HTTP navigation
    is only used to load the browser shell/static assets required to establish
    those sockets. It is never used as an authoritative source of D1 catalogue or
    wager state.
    """

    key = "1spin4win"
    display_name = "1spin4win (D1)"
    # This is an entry/lobby URL, not an HTTP catalogue API.
    catalog_url = "https://www.1spin4win.com/games"

    _CATALOG_OBSERVE_MAX_S = 15.0
    _CATALOG_QUIET_AFTER_DATA_S = 3.0
    _MAX_CAPTURED_FRAMES = 1200

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
        # Socket.IO/event-stream style frames often prefix JSON with a small
        # numeric/event marker, e.g. 42[...].
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
                for item in body:
                    if isinstance(item, dict) and str(item.get("event") or "") == "sessionStart":
                        return True
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

    def _game_from_candidate(
        self,
        data: dict[str, Any],
        *,
        source_url: str,
        path: tuple[str, ...],
    ) -> Game | None:
        context = {part.casefold() for part in path}
        game_context = bool(context & _GAME_CONTEXT_KEYS)
        explicit_game_keys = any(
            key in data
            for key in (
                "gameId",
                "game_id",
                "gameID",
                "gameName",
                "game_name",
                "launchUrl",
                "launch_url",
                "gameUrl",
                "game_url",
            )
        )

        name = _pick(data, _NAME_KEYS)
        game_id = _pick(data, _ID_KEYS)
        launch_url = _pick(data, _URL_KEYS)
        thumbnail = _pick(data, _IMAGE_KEYS)

        # A generic object with {id,name,url} is too weak. Accept it only when
        # nested below game/catalog semantics. Explicit game-specific keys may
        # qualify without that context.
        if not name:
            if not game_id or not (game_context or explicit_game_keys):
                return None
            name = _slug_title(game_id)
        if not game_id and not launch_url:
            return None
        if not (game_context or explicit_game_keys):
            return None

        slug_source = _pick(data, ("slug",)) or game_id or name
        slug = _slugify(slug_source)
        if launch_url:
            launch_url = urljoin(source_url, launch_url)
        else:
            launch_url = source_url
        if thumbnail:
            thumbnail = urljoin(source_url, thumbnail)

        return Game(
            provider=self.key,
            slug=slug,
            name=name,
            url=launch_url,
            thumbnail_url=thumbnail,
            symbol=game_id,
        )

    def _extract_games_from_ws_object(
        self,
        value: Any,
        *,
        source_url: str,
        path: tuple[str, ...] = (),
    ) -> list[Game]:
        if self._is_webvisor_payload(value):
            return []

        found: dict[str, Game] = {}

        def walk(node: Any, node_path: tuple[str, ...]) -> None:
            if isinstance(node, dict):
                game = self._game_from_candidate(node, source_url=source_url, path=node_path)
                if game is not None:
                    current = found.get(game.slug)
                    if current is None:
                        found[game.slug] = game
                    else:
                        if not current.symbol and game.symbol:
                            current.symbol = game.symbol
                        if current.url == source_url and game.url != source_url:
                            current.url = game.url
                        if not current.thumbnail_url and game.thumbnail_url:
                            current.thumbnail_url = game.thumbnail_url
                for key, child in node.items():
                    walk(child, (*node_path, str(key)))
            elif isinstance(node, list):
                for index, child in enumerate(node):
                    walk(child, (*node_path, str(index)))

        walk(value, path)
        return list(found.values())

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

    def _capture_catalog_websocket(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        on_game: GameCallback | None,
    ) -> list[Game]:
        capture_path = self.provider_root / "catalog-websocket-capture.jsonl"
        games: dict[str, Game] = {}
        records: list[dict[str, Any]] = []
        last_game_at: list[float | None] = [None]
        socket_urls: set[str] = set()

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()

                def append_record(record: dict[str, Any]) -> None:
                    if len(records) < self._MAX_CAPTURED_FRAMES:
                        records.append(record)

                def on_websocket(ws) -> None:
                    ws_url = str(ws.url)
                    socket_urls.add(ws_url)
                    noise_url = self._is_noise_websocket_url(ws_url)
                    progress(
                        f"D1 WS abierto: {ws_url}"
                        + (" [telemetría ignorada]" if noise_url else "")
                    )

                    def capture(direction: str):
                        def handler(payload) -> None:
                            decoded = self._decode_ws_json(payload)
                            telemetry = noise_url or self._is_webvisor_payload(decoded)
                            extracted: list[Game] = []
                            if not telemetry and decoded is not None:
                                extracted = self._extract_games_from_ws_object(
                                    decoded,
                                    source_url=self.catalog_url,
                                )
                                for game in extracted:
                                    existing = games.get(game.slug)
                                    is_new = existing is None
                                    if is_new:
                                        games[game.slug] = game
                                        self._persist_thumbnail(game, progress)
                                        if on_game is not None:
                                            on_game(game)
                                        last_game_at[0] = time.monotonic()
                                        progress(
                                            f"  + D1 WS catálogo: {game.name} "
                                            f"[{game.symbol or game.slug}]"
                                        )
                                    else:
                                        if not existing.symbol and game.symbol:
                                            existing.symbol = game.symbol
                                        if existing.url == self.catalog_url and game.url != self.catalog_url:
                                            existing.url = game.url
                                        if not existing.thumbnail_url and game.thumbnail_url:
                                            existing.thumbnail_url = game.thumbnail_url

                            append_record(
                                {
                                    "t": time.time(),
                                    "direction": direction,
                                    "websocket_url": ws_url,
                                    "classification": (
                                        "telemetry_ignored"
                                        if telemetry
                                        else "catalog_data" if extracted else "provider_or_unknown"
                                    ),
                                    "games_extracted": [game.slug for game in extracted],
                                    "payload": self._frame_preview(payload),
                                }
                            )

                        return handler

                    ws.on("framesent", capture("sent"))
                    ws.on("framereceived", capture("received"))

                page.on("websocket", on_websocket)
                progress(
                    "D1 catálogo: abriendo URL de entrada y esperando frames WebSocket; "
                    "HTML/HTTP no se usa como fuente de catálogo."
                )
                page.goto(
                    self.catalog_url,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )

                started = time.monotonic()
                while not stop_event.is_set():
                    elapsed = time.monotonic() - started
                    if elapsed >= self._CATALOG_OBSERVE_MAX_S:
                        break
                    if (
                        games
                        and last_game_at[0] is not None
                        and time.monotonic() - last_game_at[0] >= self._CATALOG_QUIET_AFTER_DATA_S
                    ):
                        break
                    page.wait_for_timeout(250)

                context.close()
                browser.close()
        except Exception as exc:
            append = {
                "t": time.time(),
                "classification": "capture_error",
                "error": f"{type(exc).__name__}: {exc}",
            }
            records.append(append)
            progress(f"D1 catálogo WS: {append['error']}")

        with capture_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

        diagnostic = {
            "transport": "websocket",
            "entry_url": self.catalog_url,
            "websocket_urls": sorted(socket_urls),
            "frames_captured": len(records),
            "games_extracted": len(games),
            "capture_file": str(capture_path),
            "note": (
                "D1 catalogue is sourced only from WebSocket frames. HTTP is used "
                "only for page shell/static assets needed to establish the sockets."
            ),
        }
        (self.provider_root / "catalog-websocket-summary.json").write_text(
            json.dumps(diagnostic, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return sorted(games.values(), key=lambda game: game.name.casefold())

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
    ) -> list[Game]:
        del max_pages
        games = self._capture_catalog_websocket(
            stop_event=stop_event,
            progress=progress,
            on_game=on_game,
        )
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
                        "catalog_transport": "websocket",
                    }
                    for game in games
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        progress(f"Catálogo D1 WS terminado: {len(games)} juegos únicos.")
        if not games:
            progress(
                "D1 WS: no se reconocieron objetos de catálogo. Revisá "
                "catalog-websocket-capture.jsonl; no se hará fallback a HTML."
            )
        return games

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
                            if len(frames) >= 200:
                                return
                            decoded = self._decode_ws_json(payload)
                            telemetry = noise_url or self._is_webvisor_payload(decoded)
                            frames.append(
                                {
                                    "direction": direction,
                                    "websocket_url": url,
                                    "classification": (
                                        "telemetry_ignored" if telemetry else "provider_or_unknown"
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

        payload = {
            "entry_url": entry_url,
            "transport": "websocket",
            "websocket_urls": sockets,
            "frames": frames,
            "error": error,
            "note": (
                "Passive D1 runtime observation only. Provider/game data is classified "
                "from WebSocket frames; no bet/spin frame is generated by this observer."
            ),
        }
        (attempt_dir / "runtime-websocket.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
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
        run_dir = self.game_dir(game) / "tests" / f"{stamp}-1spin4win-ws-discovery"
        attempts: list[SpinAttempt] = []
        observed_ok = 0
        errors: list[str] = []

        progress(
            f"[{game.name}] D1: catálogo/sesión/juego se tratan como WebSocket; "
            "observando socket y frames sin enviar una apuesta."
        )

        for number in range(1, repetitions + 1):
            if stop_event.is_set():
                break
            attempt_dir = run_dir / f"attempt-{number:03d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            sockets, frames, runtime_error = self._observe_game_websockets(
                game.url,
                timeout_s=timeout_s,
                attempt_dir=attempt_dir,
            )
            if sockets:
                observed_ok += 1
                attempts.append(
                    SpinAttempt(
                        number=number,
                        ok=True,
                        mode_id="WS_PROTOCOL_DISCOVERY",
                        mode_kind="DISCOVERY_WS",
                        status_code=None,
                        elapsed_ms=None,
                        symbol=game.symbol,
                        endpoint=sockets[0],
                        terminal=False,
                        wire_steps=len(frames),
                        warning=(
                            "D1 es WS-native: socket observado, pero todavía falta "
                            "clasificar handshake y frames de catalog/session/spin/bet/bonus/buy."
                        ),
                        artifact_dir=str(attempt_dir),
                    )
                )
                progress(
                    f"[{game.name}] WS observado: sockets={len(sockets)}, frames={len(frames)}"
                )
            else:
                message = runtime_error or "No se observó un WebSocket funcional de D1."
                errors.append(message)
                attempts.append(
                    SpinAttempt(
                        number=number,
                        ok=False,
                        mode_id="WS_PROTOCOL_DISCOVERY",
                        mode_kind="DISCOVERY_WS",
                        symbol=game.symbol,
                        terminal=False,
                        error=message,
                        artifact_dir=str(attempt_dir),
                    )
                )
                progress(f"[{game.name}] WS discovery ERROR: {message}")

        elapsed_total = (time.monotonic() - started) * 1000.0
        if observed_ok:
            status = "PARCIAL"
            error = (
                "Transporte D1 confirmado/modelado como WebSocket para catálogo y juego; "
                "falta automatizar handshake y frames reales de spin/bet/bonus/buy."
            )
        else:
            status = "ERROR"
            error = errors[0] if errors else "No se observó transporte WebSocket D1."

        result = GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=repetitions,
            successful_spins=0,
            failed_spins=sum(1 for attempt in attempts if not attempt.ok),
            status=status,
            symbol=game.symbol,
            discovered_modes=[
                {
                    "id": "WS_PROTOCOL_DISCOVERY",
                    "kind": "DISCOVERY_WS",
                    "transport": "websocket",
                    "catalog_transport": "websocket",
                    "provider_data_http": False,
                    "automated": True,
                    "spin_validated": False,
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
