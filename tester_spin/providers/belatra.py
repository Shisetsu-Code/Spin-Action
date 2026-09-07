from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from tester_spin.models import Game, GameTestResult, SpinAttempt, utc_now_iso
from tester_spin.providers.base import GameCallback, Progress, ProviderAdapter


_GAME_PATH_RE = re.compile(r"^/(?:en/)?games/game/([^/?#]+)/?$", re.I)
_DEMO_URL_RE = re.compile(
    r"https?://free-slot\.belatragames\.com/(?:[a-z]{2}/)?play/[A-Za-z0-9._~%+-]+",
    re.I,
)
_ENDPOINT_HINT_RE = re.compile(
    r"""(?:
        wss?://[^\s"'<>]+|
        https?://[^\s"'<>]+|
        /[A-Za-z0-9._~!$&'()*+,;=:@%/-]*(?:api|spin|bet|game|play|bonus|server|service|socket|ws)[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*
    )""",
    re.I | re.X,
)


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


class BelatraProvider(ProviderAdapter):
    key = "belatra"
    display_name = "Belatra Games"
    catalog_url = "https://belatragames.com/en/games"

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

    def _page_url(self, page: int) -> str:
        return self.catalog_url.rstrip("/") if page <= 1 else f"{self.catalog_url.rstrip('/')}/{page}"

    def _extract_catalog_page(self, html: str, base_url: str) -> list[Game]:
        soup = BeautifulSoup(html or "", "html.parser")
        found: dict[str, Game] = {}
        for anchor in soup.find_all("a", href=True):
            href = urljoin(base_url, str(anchor.get("href") or ""))
            match = _GAME_PATH_RE.match(urlparse(href).path)
            if not match:
                continue
            slug = match.group(1).strip().lower()
            if not slug or slug in found:
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
                    if candidate and candidate.casefold() not in {"play", "game", "slot"}:
                        name = candidate
                        break
            if not name:
                text = " ".join(anchor.stripped_strings).strip()
                if text and text.casefold() not in {"play", "play now"}:
                    name = text
            if not name and isinstance(container, Tag):
                heading = container.find(["h2", "h3", "h4", "h5"])
                if heading is not None:
                    candidate = " ".join(heading.stripped_strings).strip()
                    if candidate and candidate.casefold() not in {"play", "play now"}:
                        name = candidate
            if not name:
                name = _slug_title(slug)

            found[slug] = Game(
                provider=self.key,
                slug=slug,
                name=name,
                url=href,
                thumbnail_url=_best_image(img, base_url),
                symbol=slug,
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
        limit = max(1, int(max_pages))
        by_slug: dict[str, Game] = {}
        duplicate_pages = 0

        progress("Catálogo Belatra: enumerando páginas públicas oficiales.")
        for page in range(1, limit + 1):
            if stop_event.is_set():
                break
            url = self._page_url(page)
            progress(f"Belatra página {page}: {url}")
            response = self.http.get(url, timeout=30.0, allow_redirects=True)
            if response.status_code == 404:
                break
            response.raise_for_status()
            page_games = self._extract_catalog_page(response.text, response.url)
            new_count = 0
            for game in page_games:
                existing = by_slug.get(game.slug)
                if existing is not None:
                    if not existing.thumbnail_url and game.thumbnail_url:
                        existing.thumbnail_url = game.thumbnail_url
                    continue
                self._persist_thumbnail(game, progress)
                by_slug[game.slug] = game
                new_count += 1
                if on_game is not None:
                    on_game(game)
                progress(f"  + {game.name} — {game.url}")

            progress(
                f"Belatra página {page}: juegos={len(page_games)}, nuevos={new_count}, total={len(by_slug)}"
            )
            if not page_games:
                break
            duplicate_pages = duplicate_pages + 1 if new_count == 0 else 0
            if duplicate_pages >= 2:
                break

        games = sorted(by_slug.values(), key=lambda game: game.name.casefold())
        index = self.provider_root / "catalog.json"
        index.write_text(
            json.dumps([game.__dict__ if hasattr(game, "__dict__") else {
                "provider": game.provider,
                "slug": game.slug,
                "name": game.name,
                "url": game.url,
                "thumbnail_url": game.thumbnail_url,
                "thumbnail_path": game.thumbnail_path,
                "symbol": game.symbol,
            } for game in games], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        progress(f"Catálogo Belatra terminado: {len(games)} juegos únicos.")
        return games

    def _resolve_demo_url(self, game: Game, detail_html: str) -> str:
        match = _DEMO_URL_RE.search(detail_html or "")
        if match:
            return match.group(0)
        return f"https://free-slot.belatragames.com/play/{game.slug}"

    @staticmethod
    def _endpoint_candidates(text: str, base_url: str) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for match in _ENDPOINT_HINT_RE.finditer(text or ""):
            value = match.group(0).rstrip(".,);]}")
            if value.startswith("/"):
                value = urljoin(base_url, value)
            if value not in seen:
                seen.add(value)
                out.append(value)
            if len(out) >= 300:
                break
        return out

    def _discover_demo_protocol(
        self,
        game: Game,
        *,
        timeout_s: float,
        attempt_dir: Path,
    ) -> tuple[str, float, list[str], list[str]]:
        started = time.monotonic()
        detail = self.http.get(game.url, timeout=timeout_s, allow_redirects=True)
        detail.raise_for_status()
        demo_url = self._resolve_demo_url(game, detail.text)
        demo = self.http.get(demo_url, timeout=timeout_s, allow_redirects=True)
        demo.raise_for_status()
        elapsed_ms = (time.monotonic() - started) * 1000.0

        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "detail-page.html").write_text(detail.text, encoding="utf-8", errors="replace")
        (attempt_dir / "demo-page.html").write_text(demo.text, encoding="utf-8", errors="replace")

        soup = BeautifulSoup(demo.text or "", "html.parser")
        scripts: list[str] = []
        endpoints = self._endpoint_candidates(demo.text, demo.url)
        for node in soup.find_all("script", src=True):
            src = urljoin(demo.url, str(node.get("src") or ""))
            if src and src not in scripts:
                scripts.append(src)
            if len(scripts) >= 12:
                break

        scanned_scripts: list[str] = []
        for src in scripts:
            try:
                response = self.http.get(src, timeout=min(timeout_s, 20.0))
                response.raise_for_status()
                if len(response.content) > 4 * 1024 * 1024:
                    continue
                scanned_scripts.append(src)
                for candidate in self._endpoint_candidates(response.text, response.url):
                    if candidate not in endpoints:
                        endpoints.append(candidate)
                        if len(endpoints) >= 500:
                            break
            except Exception:
                continue

        (attempt_dir / "bootstrap-discovery.json").write_text(
            json.dumps(
                {
                    "game": game.name,
                    "catalog_url": game.url,
                    "demo_url": demo.url,
                    "detail_status": detail.status_code,
                    "demo_status": demo.status_code,
                    "elapsed_ms": round(elapsed_ms, 2),
                    "scripts": scripts,
                    "scripts_scanned": scanned_scripts,
                    "endpoint_candidates": endpoints,
                    "note": (
                        "Bootstrap/discovery only. A spin is not considered validated until "
                        "the observable Belatra action protocol is implemented from capture evidence."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return demo.url, elapsed_ms, scripts, endpoints

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
        run_dir = self.game_dir(game) / "tests" / f"{stamp}-belatra-discovery"
        attempts: list[SpinAttempt] = []
        bootstrap_ok = 0
        errors: list[str] = []

        progress(
            f"[{game.name}] Belatra: bootstrap + descubrimiento de protocolo; "
            "spin aún no se marca OK sin captura observable."
        )
        for number in range(1, repetitions + 1):
            if stop_event.is_set():
                break
            attempt_dir = run_dir / f"attempt-{number:03d}"
            try:
                demo_url, elapsed_ms, _scripts, endpoints = self._discover_demo_protocol(
                    game,
                    timeout_s=timeout_s,
                    attempt_dir=attempt_dir,
                )
                bootstrap_ok += 1
                progress(
                    f"[{game.name}] bootstrap {number}/{repetitions}: "
                    f"{elapsed_ms:.0f} ms, candidatos={len(endpoints)}"
                )
                attempts.append(
                    SpinAttempt(
                        number=number,
                        ok=True,
                        mode_id="BOOTSTRAP_DISCOVERY",
                        mode_kind="DISCOVERY",
                        status_code=200,
                        elapsed_ms=elapsed_ms,
                        symbol=game.slug,
                        endpoint=demo_url,
                        na="",
                        terminal=False,
                        wire_steps=1,
                        warning="Belatra spin protocol pendiente de captura/automatización.",
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
                        symbol=game.slug,
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
                "Demo Belatra accesible y protocolo candidato recolectado; "
                "falta automatizar la acción de spin/bonus/buy desde evidencia HAR/runtime."
            )
        else:
            status = "ERROR"
            error = errors[0] if errors else "No se pudo completar bootstrap Belatra."

        result = GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=repetitions,
            successful_spins=0,
            failed_spins=sum(1 for attempt in attempts if not attempt.ok),
            status=status,
            symbol=game.slug,
            discovered_modes=[
                {
                    "id": "BOOTSTRAP_DISCOVERY",
                    "kind": "DISCOVERY",
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
