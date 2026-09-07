from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from tester_spin.models import Game, GameTestResult, SpinAttempt, utc_now_iso
from tester_spin.providers.base import GameCallback, Progress, ProviderAdapter


_GAME_PATH_RE = re.compile(r"^/(?:[a-z]{2}/)?games/([^/?#]+)/?$", re.I)
_DEMO_HOST_RE = re.compile(r"^https?://gs\.1spin4win\.com(?:/|$)", re.I)
_ENDPOINT_HINT_RE = re.compile(
    r"""(?:
        wss?://[^\s"'<>]+|
        https?://[^\s"'<>]+|
        /[A-Za-z0-9._~!$&'()*+,;=:@%/-]*(?:api|spin|bet|game|play|bonus|server|service|socket|ws)[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*
    )""",
    re.I | re.X,
)
_ONE_SPIN_LOCAL = threading.local()


def _safe_folder(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", str(name).strip()).rstrip(" .")
    return value[:160] or "Unnamed Game"


def _slug_title(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


def _best_image(img: Tag | None, base_url: str) -> str:
    if img is None:
        return ""
    for key in ("data-src", "data-lazy-src", "src"):
        value = str(img.get(key) or "").strip()
        if value and not value.startswith("data:"):
            return urljoin(base_url, value)
    srcset = str(img.get("srcset") or img.get("data-srcset") or "")
    candidates = [part.strip().split()[0] for part in srcset.split(",") if part.strip()]
    if candidates:
        return urljoin(base_url, candidates[-1])
    return ""


class OneSpin4WinProvider(ProviderAdapter):
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

    def _extract_catalog_page(self, html: str, base_url: str) -> list[Game]:
        soup = BeautifulSoup(html or "", "html.parser")
        found: dict[str, Game] = {}
        for anchor in soup.find_all("a", href=True):
            href = urljoin(base_url, str(anchor.get("href") or ""))
            parsed = urlparse(href)
            if parsed.netloc and parsed.netloc.casefold() not in {
                "1spin4win.com",
                "www.1spin4win.com",
            }:
                continue
            match = _GAME_PATH_RE.match(parsed.path)
            if not match:
                continue
            slug = match.group(1).strip().lower()
            if not slug or slug in {"category", "filter"} or slug in found:
                continue

            container: Tag | None = anchor if isinstance(anchor, Tag) else None
            for parent in anchor.parents:
                if isinstance(parent, Tag) and parent.name in {"article", "li", "div"}:
                    container = parent
                    if parent.find("img") is not None:
                        break

            img = container.find("img") if isinstance(container, Tag) else anchor.find("img")
            name = ""
            if img is not None:
                for key in ("alt", "title"):
                    candidate = str(img.get(key) or "").strip()
                    if candidate and candidate.casefold() not in {"play", "demo", "details", "image"}:
                        name = candidate
                        break
            if not name:
                text = " ".join(anchor.stripped_strings).strip()
                if text and text.casefold() not in {"demo play", "try game demo", "details"}:
                    name = text
            if not name and isinstance(container, Tag):
                heading = container.find(["h2", "h3", "h4", "h5"])
                if heading is not None:
                    name = " ".join(heading.stripped_strings).strip()
            if not name:
                name = _slug_title(slug)

            found[slug] = Game(
                provider=self.key,
                slug=slug,
                name=name,
                url=href,
                thumbnail_url=_best_image(img, base_url),
                symbol="",
            )
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
            response = self.http.get(game.thumbnail_url, timeout=20.0)
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
        del max_pages
        if stop_event.is_set():
            return []
        progress("Catálogo 1spin4win (D1): leyendo portfolio oficial.")
        response = self.http.get(self.catalog_url, timeout=30.0, allow_redirects=True)
        response.raise_for_status()
        games = self._extract_catalog_page(response.text, response.url)
        for game in games:
            if stop_event.is_set():
                break
            self._persist_thumbnail(game, progress)
            if on_game is not None:
                on_game(game)
            progress(f"  + {game.name} — {game.url}")

        games = sorted(games, key=lambda game: game.name.casefold())
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
                    }
                    for game in games
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        progress(f"Catálogo 1spin4win terminado: {len(games)} juegos únicos.")
        return games

    @staticmethod
    def _extract_game_id(html: str) -> str:
        soup = BeautifulSoup(html or "", "html.parser")
        strings = [value.strip() for value in soup.stripped_strings if value.strip()]
        for index, value in enumerate(strings):
            if value.casefold() in {"game id", "id del juego", "id de jeu", "id do jogo"}:
                for candidate in strings[index + 1 : index + 5]:
                    normalized = re.sub(r"\s+", "", candidate)
                    if re.fullmatch(r"[A-Za-z0-9_-]{4,120}", normalized):
                        return normalized
        patterns = (
            r'"gameId"\s*:\s*"([A-Za-z0-9_-]{4,120})"',
            r'"game_id"\s*:\s*"([A-Za-z0-9_-]{4,120})"',
            r'data-game-id=["\']([A-Za-z0-9_-]{4,120})["\']',
        )
        for pattern in patterns:
            match = re.search(pattern, html or "", re.I)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _extract_demo_url(html: str, base_url: str) -> str:
        soup = BeautifulSoup((html or "").replace("\\/", "/"), "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = urljoin(base_url, str(anchor.get("href") or "").replace("\\/", "/"))
            if _DEMO_HOST_RE.match(href):
                return href
        match = re.search(
            r'https?://gs\.1spin4win\.com/[^\s"\'<>]+',
            (html or "").replace("\\/", "/"),
            re.I,
        )
        return match.group(0).rstrip(".,);]}") if match else ""

    @staticmethod
    def _network_candidates(text: str, base_url: str) -> list[str]:
        normalized_text = (text or "").replace("\\/", "/")
        seen: set[str] = set()
        out: list[str] = []
        for match in _ENDPOINT_HINT_RE.finditer(normalized_text):
            value = match.group(0).rstrip(".,);]}")
            if value.startswith("/"):
                value = urljoin(base_url, value)
            if value not in seen:
                seen.add(value)
                out.append(value)
            if len(out) >= 800:
                break
        return out

    @classmethod
    def _websocket_candidates(cls, text: str, base_url: str) -> list[str]:
        return [
            value
            for value in cls._network_candidates(text, base_url)
            if value.lower().startswith(("ws://", "wss://"))
        ]

    @classmethod
    def _http_bootstrap_candidates(cls, text: str, base_url: str) -> list[str]:
        return [
            value
            for value in cls._network_candidates(text, base_url)
            if value.lower().startswith(("http://", "https://"))
        ]

    def _discover_demo_protocol(
        self,
        game: Game,
        *,
        timeout_s: float,
        attempt_dir: Path,
    ) -> tuple[str, str, int, float, list[str], list[str], list[str]]:
        started = time.monotonic()
        session = self._worker_session()
        detail = session.get(game.url, timeout=timeout_s, allow_redirects=True)
        detail.raise_for_status()

        game_id = self._extract_game_id(detail.text)
        demo_url = self._extract_demo_url(detail.text, detail.url)
        if not demo_url:
            raise RuntimeError("la ficha oficial no expuso una URL demo gs.1spin4win.com")

        demo = session.get(demo_url, timeout=timeout_s, allow_redirects=True)
        demo.raise_for_status()
        elapsed_ms = (time.monotonic() - started) * 1000.0

        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "detail-page.html").write_text(detail.text, encoding="utf-8", errors="replace")
        (attempt_dir / "demo-page.html").write_text(demo.text, encoding="utf-8", errors="replace")

        soup = BeautifulSoup(demo.text or "", "html.parser")
        scripts: list[str] = []
        websocket_candidates = self._websocket_candidates(demo.text, demo.url)
        http_candidates = self._http_bootstrap_candidates(demo.text, demo.url)
        for node in soup.find_all("script", src=True):
            src = urljoin(demo.url, str(node.get("src") or ""))
            if src and src not in scripts:
                scripts.append(src)
            if len(scripts) >= 16:
                break

        scanned_scripts: list[str] = []
        for src in scripts:
            try:
                response = session.get(src, timeout=min(timeout_s, 20.0))
                response.raise_for_status()
                if len(response.content) > 5 * 1024 * 1024:
                    continue
                scanned_scripts.append(src)
                for candidate in self._websocket_candidates(response.text, response.url):
                    if candidate not in websocket_candidates:
                        websocket_candidates.append(candidate)
                        if len(websocket_candidates) >= 200:
                            break
                for candidate in self._http_bootstrap_candidates(response.text, response.url):
                    if candidate not in http_candidates:
                        http_candidates.append(candidate)
                        if len(http_candidates) >= 400:
                            break
            except Exception:
                continue

        (attempt_dir / "bootstrap-discovery.json").write_text(
            json.dumps(
                {
                    "provider": self.key,
                    "game": game.name,
                    "game_id": game_id,
                    "catalog_url": game.url,
                    "demo_url": demo.url,
                    "detail_status": detail.status_code,
                    "demo_status": demo.status_code,
                    "elapsed_ms": round(elapsed_ms, 2),
                    "transport": {
                        "bootstrap": "http",
                        "bets_and_spins": "websocket",
                    },
                    "scripts": scripts,
                    "scripts_scanned": scanned_scripts,
                    "websocket_candidates": websocket_candidates,
                    "http_bootstrap_candidates": http_candidates,
                    "note": (
                        "1spin4win/D1 uses WebSocket transport for bets/spins. HTTP is "
                        "bootstrap/configuration only. WS frame schema and state transitions "
                        "remain unvalidated until implemented from observed runtime capture."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return (
            demo.url,
            game_id,
            int(demo.status_code),
            elapsed_ms,
            scripts,
            websocket_candidates,
            http_candidates,
        )

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
        run_dir = self.game_dir(game) / "tests" / f"{stamp}-1spin4win-discovery"
        attempts: list[SpinAttempt] = []
        bootstrap_ok = 0
        resolved_id = game.symbol
        errors: list[str] = []

        progress(
            f"[{game.name}] 1spin4win/D1: HTTP sólo para bootstrap; "
            "apuestas/tiradas se descubren como transporte WebSocket."
        )
        for number in range(1, repetitions + 1):
            if stop_event.is_set():
                break
            attempt_dir = run_dir / f"attempt-{number:03d}"
            try:
                (
                    demo_url,
                    game_id,
                    demo_status,
                    elapsed_ms,
                    _scripts,
                    websocket_candidates,
                    http_candidates,
                ) = (
                    self._discover_demo_protocol(
                        game,
                        timeout_s=timeout_s,
                        attempt_dir=attempt_dir,
                    )
                )
                bootstrap_ok += 1
                resolved_id = game_id or resolved_id
                progress(
                    f"[{game.name}] bootstrap {number}/{repetitions}: "
                    f"{elapsed_ms:.0f} ms, gameId={resolved_id or '—'}, "
                    f"WS={len(websocket_candidates)}, HTTP-bootstrap={len(http_candidates)}"
                )
                attempts.append(
                    SpinAttempt(
                        number=number,
                        ok=True,
                        mode_id="BOOTSTRAP_DISCOVERY",
                        mode_kind="DISCOVERY_WS",
                        status_code=demo_status,
                        elapsed_ms=elapsed_ms,
                        symbol=resolved_id,
                        endpoint=(websocket_candidates[0] if websocket_candidates else demo_url),
                        terminal=False,
                        wire_steps=1,
                        warning=(
                            "1spin4win/D1 usa WebSocket para apuestas/tiradas; "
                            "falta automatizar handshake y frames de acción/respuesta."
                        ),
                        artifact_dir=str(attempt_dir),
                    )
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                errors.append(message)
                attempts.append(
                    SpinAttempt(
                        number=number,
                        ok=False,
                        mode_id="BOOTSTRAP_DISCOVERY",
                        mode_kind="DISCOVERY",
                        symbol=resolved_id,
                        terminal=False,
                        error=message,
                        artifact_dir=str(attempt_dir),
                    )
                )
                progress(f"[{game.name}] bootstrap {number}/{repetitions}: ERROR {message}")

        elapsed_total = (time.monotonic() - started) * 1000.0
        if bootstrap_ok:
            status = "PARCIAL"
            error = (
                "Bootstrap HTTP de 1spin4win accesible; apuestas/tiradas son WebSocket. "
                "Falta automatizar handshake, frames spin/bet/bonus/buy y transiciones "
                "desde evidencia runtime."
            )
        else:
            status = "ERROR"
            error = errors[0] if errors else "No se pudo completar bootstrap 1spin4win."

        result = GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=repetitions,
            successful_spins=0,
            failed_spins=sum(1 for attempt in attempts if not attempt.ok),
            status=status,
            symbol=resolved_id,
            discovered_modes=[
                {
                    "id": "BOOTSTRAP_DISCOVERY",
                    "kind": "DISCOVERY_WS",
                    "transport": "websocket",
                    "http_role": "bootstrap_only",
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
