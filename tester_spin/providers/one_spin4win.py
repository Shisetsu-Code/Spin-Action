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
        run_dir = self.game_dir(game) / "tests" / f"{stamp}-1spin4win-ws-discovery"
        attempts: list[SpinAttempt] = []
        observed_ok = 0
        errors: list[str] = []

        progress(
            f"[{game.name}] D1: abriendo demo observado en catálogo y capturando "
            "el protocolo WebSocket."
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
                        symbol=game.symbol,
                        endpoint=sockets[0],
                        terminal=False,
                        wire_steps=len(frames),
                        warning=(
                            "Socket D1 observado; falta clasificar handshake y frames "
                            "spin/bet/bonus/buy antes de marcar una tirada como OK."
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
        status = "PARCIAL" if observed_ok else "ERROR"
        error = (
            "Catálogo D1 resuelto desde Webflow; runtime WS observado. Falta "
            "automatizar handshake y frames reales de spin/bet/bonus/buy."
            if observed_ok
            else (errors[0] if errors else "No se observó transporte WebSocket D1.")
        )

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
                    "catalog_transport": "webflow_html",
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
