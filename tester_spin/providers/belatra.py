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


_GAME_PATH_RE = re.compile(r"^/(?:[a-z]{2}/)?games/game/([^/?#]+)/?$", re.I)
_DEMO_URL_RE = re.compile(
    r"https?://free-slot\.belatragames\.com/(?:[a-z]{2}/)?play/[A-Za-z0-9._~%+-]+",
    re.I,
)
_BELATRA_LOCAL = threading.local()

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


_NUMBER_WORDS = {
    0: "zero",
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
    17: "seventeen",
    18: "eighteen",
    19: "nineteen",
    20: "twenty",
}


def _slugify_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")


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
    # Slot category observed in the supplied Belatra HAR.
    catalog_url = "https://belatragames.com/es/games/category/2"

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
        session = getattr(_BELATRA_LOCAL, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(dict(self.http.headers))
            try:
                session.cookies.update(self.http.cookies.get_dict())
            except Exception:
                pass
            _BELATRA_LOCAL.session = session
        return session

    def _page_url(self, page: int) -> str:
        root = self.catalog_url.rstrip("/")
        return root if page <= 1 else f"{root}/{page}"

    @staticmethod
    def _next_stream(payload: str) -> str:
        """Decode and concatenate Next.js RSC chunks from a full HTML response."""
        text = payload or ""
        if "self.__next_f.push" not in text:
            return text

        soup = BeautifulSoup(text, "html.parser")
        chunks: list[str] = []
        for node in soup.find_all("script"):
            script = node.string or node.get_text() or ""
            if "self.__next_f.push" not in script:
                continue
            match = re.search(
                r'self\.__next_f\.push\(\[1,"(.*)"\]\)\s*$',
                script,
                re.S,
            )
            if not match:
                continue
            try:
                chunks.append(json.loads('"' + match.group(1) + '"'))
            except json.JSONDecodeError:
                continue
        return "".join(chunks) if chunks else text

    @staticmethod
    def _game_objects(stream: str) -> list[dict[str, Any]]:
        """Return the actual catalogue games[] array, ignoring homonymous fields."""
        needle = '"games":'
        offset = 0
        decoder = json.JSONDecoder()
        best: list[dict[str, Any]] = []
        while True:
            index = stream.find(needle, offset)
            if index < 0:
                return best
            try:
                value, _end = decoder.raw_decode(stream[index + len(needle) :])
            except json.JSONDecodeError:
                offset = index + len(needle)
                continue
            if isinstance(value, list):
                candidates = [
                    item
                    for item in value
                    if isinstance(item, dict)
                    and str(item.get("slug") or "").strip()
                    and str(item.get("title") or "").strip()
                ]
                if len(candidates) > len(best):
                    best = candidates
            offset = index + len(needle)

    @staticmethod
    def _pagination_meta(stream: str) -> dict[str, Any]:
        needle = '"meta":'
        offset = 0
        decoder = json.JSONDecoder()
        while True:
            index = stream.find(needle, offset)
            if index < 0:
                return {}
            try:
                value, _end = decoder.raw_decode(stream[index + len(needle) :])
            except json.JSONDecodeError:
                offset = index + len(needle)
                continue
            if (
                isinstance(value, dict)
                and "current_page" in value
                and "last_page" in value
                and "per_page" in value
            ):
                return value
            offset = index + len(needle)

    @staticmethod
    def _best_api_image(item: dict[str, Any]) -> str:
        image = item.get("image")
        if not isinstance(image, dict):
            return ""
        for viewport in ("desktop", "tablet", "mobile"):
            variants = image.get(viewport)
            if not isinstance(variants, dict):
                continue
            for key in ("webp_x2", "x2", "webp_x1", "x1"):
                value = str(variants.get(key) or "").strip()
                if value:
                    return value
        return ""

    @staticmethod
    def _catalog_language(base_url: str) -> str:
        parts = [part for part in urlparse(base_url).path.split("/") if part]
        if parts and re.fullmatch(r"[a-z]{2}", parts[0], re.I):
            return parts[0].lower()
        return "en"

    def _extract_catalog_page(
        self,
        payload: str,
        base_url: str,
    ) -> tuple[list[Game], dict[str, Any]]:
        stream = self._next_stream(payload)
        raw_games = self._game_objects(stream)
        meta = self._pagination_meta(stream)
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        language = self._catalog_language(base_url)

        found: dict[str, Game] = {}
        for item in raw_games:
            slug = str(item.get("slug") or "").strip().lower()
            if not slug or slug in found:
                continue
            name = str(item.get("title") or "").strip() or _slug_title(slug)
            provider_id = str(item.get("id") or "").strip()
            found[slug] = Game(
                provider=self.key,
                slug=slug,
                name=name,
                url=f"{origin}/{language}/games/game/{slug}",
                thumbnail_url=self._best_api_image(item),
                symbol=provider_id or slug,
            )

        return list(found.values()), meta

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
        expected_last_page: int | None = None
        expected_total: int | None = None

        progress(
            "Catálogo Belatra Next.js: categoría 2 (slots), leyendo objects games[] "
            "del stream RSC y siguiendo current_page/last_page."
        )

        for page in range(1, limit + 1):
            if stop_event.is_set():
                break

            url = self._page_url(page)
            progress(f"Belatra categoría 2 página {page}: {url}")
            response = self.http.get(url, timeout=30.0, allow_redirects=True)
            if response.status_code == 404:
                break
            response.raise_for_status()

            page_games, meta = self._extract_catalog_page(response.text, response.url)
            if meta:
                try:
                    expected_last_page = int(meta.get("last_page") or 0) or expected_last_page
                except (TypeError, ValueError):
                    pass
                try:
                    expected_total = int(meta.get("total") or 0) or expected_total
                except (TypeError, ValueError):
                    pass

            new_count = 0
            for game in page_games:
                existing = by_slug.get(game.slug)
                if existing is not None:
                    if not existing.thumbnail_url and game.thumbnail_url:
                        existing.thumbnail_url = game.thumbnail_url
                    if (not existing.symbol or existing.symbol == existing.slug) and game.symbol:
                        existing.symbol = game.symbol
                    continue

                self._persist_thumbnail(game, progress)
                by_slug[game.slug] = game
                new_count += 1
                if on_game is not None:
                    on_game(game)
                progress(f"  + {game.name} [id={game.symbol}] — {game.url}")

            current_page = meta.get("current_page") if meta else page
            last_page = meta.get("last_page") if meta else expected_last_page
            total = meta.get("total") if meta else expected_total
            progress(
                f"Belatra página {page}: juegos={len(page_games)}, nuevos={new_count}, "
                f"total_local={len(by_slug)}, current={current_page}, "
                f"last={last_page or '—'}, total_remoto={total or '—'}"
            )

            if not page_games:
                break

            duplicate_pages = duplicate_pages + 1 if new_count == 0 else 0
            if duplicate_pages >= 2:
                progress("Belatra: dos páginas consecutivas sin juegos nuevos; fin defensivo.")
                break

            if expected_last_page is not None and page >= expected_last_page:
                break

            per_page = 0
            if meta:
                try:
                    per_page = int(meta.get("per_page") or 0)
                except (TypeError, ValueError):
                    per_page = 0
            if per_page > 0 and len(page_games) < per_page:
                break

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
                        "catalog_transport": "nextjs_rsc",
                        "category_id": 2,
                    }
                    for game in games
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        progress(
            f"Catálogo Belatra terminado: {len(games)} juegos únicos"
            + (f" / remoto={expected_total}" if expected_total is not None else "")
            + "."
        )
        return games

    @staticmethod
    def _extract_demo_links(text: str, base_url: str = "https://free-slot.belatragames.com/") -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        patterns = (
            r"""https?://free-slot\.belatragames\.com/(?:[a-z]{2}/)?play/[A-Za-z0-9._~%+-]+""",
            r"""(?<![A-Za-z0-9])/(?:[a-z]{2}/)?play/[A-Za-z0-9._~%+-]+""",
            r"""(?<![A-Za-z0-9/])play/[A-Za-z0-9._~%+-]+""",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, text or "", re.I):
                value = match.group(0).strip()
                if value.startswith("play/"):
                    value = "/" + value
                value = urljoin(base_url, value)
                if value not in seen:
                    seen.add(value)
                    found.append(value)
        return found

    @staticmethod
    def _extract_nickname(text: str) -> str:
        soup = BeautifulSoup(text or "", "html.parser")
        lines = [
            " ".join(part.split())
            for part in soup.get_text("\n", strip=True).splitlines()
            if " ".join(part.split())
        ]
        for index, line in enumerate(lines):
            normalized = line.casefold().rstrip(":")
            if normalized == "nickname" and index + 1 < len(lines):
                candidate = re.sub(r"[^A-Za-z0-9_-]+", "", lines[index + 1]).strip()
                if candidate:
                    return candidate
            match = re.match(r"nickname\s*:\s*([A-Za-z0-9_-]+)", line, re.I)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _demo_slug_candidates(game: Game) -> list[str]:
        seeds = [game.slug, _slugify_text(game.name)]
        found: list[str] = []
        seen: set[str] = set()

        def add(value: str) -> None:
            value = _slugify_text(value)
            if value and value not in seen:
                seen.add(value)
                found.append(value)

        for seed in seeds:
            add(seed)
            match = re.match(r"^(\d+)-(.*)$", seed)
            if not match:
                continue
            number_text, tail = match.groups()
            add(tail)
            add(f"{seed}-{number_text}")
            try:
                number = int(number_text)
            except ValueError:
                continue
            word = _NUMBER_WORDS.get(number)
            if word:
                add(f"{word}-{tail}")

        return found

    @classmethod
    def _promotion_pack_candidates(cls, game: Game) -> list[str]:
        base = "https://free-slot.belatragames.com/promotion-packs/"
        slugs = cls._demo_slug_candidates(game)
        found: list[str] = []
        seen: set[str] = set()
        for slug in slugs:
            for candidate in (slug, f"{slug}-1"):
                url = base + candidate
                if url not in seen:
                    seen.add(url)
                    found.append(url)
        return found

    def _resolve_demo_response(
        self,
        session: requests.Session,
        game: Game,
        detail_html: str,
        *,
        timeout_s: float,
        attempt_dir: Path,
    ) -> requests.Response:
        """Resolve a real free-slot page without assuming corporate slug equality.

        Belatra has historical aliases where the corporate catalogue slug differs
        from the free-slot slug. Examples observed publicly:
          20-icy-fruits -> icy-fruits
          7-fruits      -> seven-fruits
          88-golden     -> 88-golden-88

        Every candidate is validated by an actual HTTP response. A 404 only rejects
        that candidate; it no longer aborts the whole game.
        """
        attempted: list[dict[str, Any]] = []
        candidates: list[str] = []
        seen: set[str] = set()

        def add(url: str, source: str) -> None:
            url = str(url or "").strip()
            if not url:
                return
            if url.startswith("/"):
                url = urljoin("https://free-slot.belatragames.com/", url)
            parsed = urlparse(url)
            if (parsed.hostname or "").casefold() != "free-slot.belatragames.com":
                return
            if "/play/" not in parsed.path.casefold():
                return
            if url in seen:
                return
            seen.add(url)
            candidates.append(url)
            attempted.append({"url": url, "source": source, "status": None})

        for url in self._extract_demo_links(detail_html, game.url):
            add(url, "detail")

        for slug in self._demo_slug_candidates(game):
            add(f"https://free-slot.belatragames.com/play/{slug}", "derived")
            add(f"https://free-slot.belatragames.com/es/play/{slug}", "derived_es")

        def probe_pending() -> requests.Response | None:
            for item in attempted:
                if item["status"] is not None:
                    continue
                url = str(item["url"])
                try:
                    response = session.get(url, timeout=timeout_s, allow_redirects=True)
                    item["status"] = int(response.status_code)
                    item["final_url"] = response.url
                    if response.status_code < 400 and "/play/" in urlparse(response.url).path.casefold():
                        item["selected"] = True
                        return response
                except Exception as exc:
                    item["error"] = f"{type(exc).__name__}: {exc}"
                    item["status"] = -1
            return None

        response = probe_pending()
        promo_pages: list[dict[str, Any]] = []

        if response is None:
            for promo_url in self._promotion_pack_candidates(game):
                entry: dict[str, Any] = {"url": promo_url}
                promo_pages.append(entry)
                try:
                    promo = session.get(promo_url, timeout=timeout_s, allow_redirects=True)
                    entry["status"] = int(promo.status_code)
                    entry["final_url"] = promo.url
                    if promo.status_code >= 400:
                        continue

                    for link in self._extract_demo_links(promo.text, promo.url):
                        add(link, "promotion_pack_link")

                    nickname = self._extract_nickname(promo.text)
                    if nickname:
                        entry["nickname"] = nickname
                        normalized = _slugify_text(nickname.replace("_", "-"))
                        add(
                            f"https://free-slot.belatragames.com/play/{normalized}",
                            "promotion_pack_nickname",
                        )
                        add(
                            f"https://free-slot.belatragames.com/es/play/{normalized}",
                            "promotion_pack_nickname_es",
                        )
                except Exception as exc:
                    entry["status"] = -1
                    entry["error"] = f"{type(exc).__name__}: {exc}"

            response = probe_pending()

        resolution = {
            "game": game.name,
            "catalog_slug": game.slug,
            "attempted_play_urls": attempted,
            "promotion_pack_pages": promo_pages,
            "selected_url": response.url if response is not None else "",
        }
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "demo-resolution.json").write_text(
            json.dumps(resolution, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if response is None:
            statuses = ", ".join(
                f"{item.get('url')}={item.get('status')}"
                for item in attempted[:12]
            )
            raise RuntimeError(
                "Belatra: no se resolvió una demo válida en free-slot"
                + (f" ({statuses})" if statuses else "")
            )
        return response

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

    @staticmethod
    def _runtime_noise_url(url: str) -> bool:
        host = (urlparse(url).hostname or "").casefold()
        return (
            host.endswith("yandex.ru")
            or host.endswith("yandex.net")
            or "metrika" in host
            or host.endswith("google-analytics.com")
            or host.endswith("googletagmanager.com")
            or host.endswith("doubleclick.net")
        )

    @staticmethod
    def _runtime_payload_preview(payload: object, limit: int = 16384) -> dict[str, Any]:
        if isinstance(payload, bytes):
            raw = payload[:limit]
            return {
                "kind": "binary",
                "size": len(payload),
                "hex": raw.hex(),
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
    def _runtime_signal_summary(events: list[dict[str, Any]]) -> dict[str, int]:
        action_requests = 0
        action_non_get = 0
        action_xhr_fetch = 0
        action_ws_sent = 0
        action_ws_received = 0
        for event in events:
            if event.get("phase") != "action" or event.get("noise"):
                continue
            kind = str(event.get("kind") or "")
            if kind == "request":
                action_requests += 1
                if str(event.get("method") or "GET").upper() != "GET":
                    action_non_get += 1
                if str(event.get("resource_type") or "") in {"xhr", "fetch"}:
                    action_xhr_fetch += 1
            elif kind == "ws_frame":
                if event.get("direction") == "sent":
                    action_ws_sent += 1
                elif event.get("direction") == "received":
                    action_ws_received += 1
        unique_request_signals = 0
        for event in events:
            if event.get("phase") != "action" or event.get("noise"):
                continue
            if event.get("kind") != "request":
                continue
            method = str(event.get("method") or "GET").upper()
            resource_type = str(event.get("resource_type") or "")
            if method != "GET" or resource_type in {"xhr", "fetch"}:
                unique_request_signals += 1

        return {
            "action_requests": action_requests,
            "action_non_get": action_non_get,
            "action_xhr_fetch": action_xhr_fetch,
            "action_ws_sent": action_ws_sent,
            "action_ws_received": action_ws_received,
            "action_signals": unique_request_signals + action_ws_sent,
        }

    @staticmethod
    def _try_runtime_action(page) -> dict[str, Any]:
        """Try a conservative demo-only spin input and report exactly what was attempted.

        This does not prove a spin. The runtime capture later decides only whether
        network activity changed after the input. No result is marked OK here.
        """
        labels = re.compile(r"^(?:spin|start|girar|tirar)$", re.I)
        frames = list(page.frames)

        # Prefer an explicit DOM control if a game exposes one outside its canvas.
        for frame in reversed(frames):
            try:
                locator = frame.get_by_text(labels, exact=True)
                count = min(locator.count(), 8)
                for index in range(count):
                    item = locator.nth(index)
                    if not item.is_visible():
                        continue
                    item.click(timeout=1200)
                    return {
                        "kind": "dom_control",
                        "frame_url": frame.url,
                        "selector": "exact visible text: spin/start/girar/tirar",
                    }
            except Exception:
                continue

        # Most Belatra HTML5 titles render controls inside canvas. Focus the
        # largest visible canvas and send Space, a common keyboard spin binding.
        best: tuple[float, Any, Any, dict[str, float]] | None = None
        for frame in reversed(frames):
            try:
                canvases = frame.locator("canvas")
                for index in range(min(canvases.count(), 12)):
                    canvas = canvases.nth(index)
                    if not canvas.is_visible():
                        continue
                    box = canvas.bounding_box()
                    if not box:
                        continue
                    area = float(box["width"]) * float(box["height"])
                    if best is None or area > best[0]:
                        best = (area, frame, canvas, box)
            except Exception:
                continue

        if best is not None:
            _area, frame, canvas, box = best
            try:
                canvas.click(
                    position={
                        "x": max(1.0, float(box["width"]) / 2.0),
                        "y": max(1.0, float(box["height"]) / 2.0),
                    },
                    timeout=1200,
                )
            except Exception:
                pass
            page.keyboard.press("Space")
            return {
                "kind": "canvas_focus_plus_space",
                "frame_url": frame.url,
                "canvas_width": round(float(box["width"]), 2),
                "canvas_height": round(float(box["height"]), 2),
            }

        # Last diagnostic fallback. This can merely scroll the page; therefore the
        # action remains unvalidated until correlated traffic is observed.
        page.keyboard.press("Space")
        return {"kind": "page_space", "frame_url": page.url}

    def _capture_runtime_action(
        self,
        demo_url: str,
        *,
        timeout_s: float,
        attempt_dir: Path,
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        sockets: list[str] = []
        report: dict[str, Any] = {
            "demo_url": demo_url,
            "action": {},
            "summary": {},
            "websocket_urls": sockets,
            "events": events,
            "error": "",
            "note": (
                "Diagnostic runtime correlation only. An input is attempted on the "
                "public free-play demo, but no Belatra spin is considered validated "
                "until the resulting protocol signature is classified."
            ),
        }
        started = time.monotonic()
        phase = {"value": "bootstrap"}

        def elapsed_ms() -> float:
            return round((time.monotonic() - started) * 1000.0, 2)

        def append(event: dict[str, Any]) -> None:
            if len(events) >= 1600:
                return
            event.setdefault("t_ms", elapsed_ms())
            event.setdefault("phase", phase["value"])
            events.append(event)

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    ignore_https_errors=True,
                )
                page = context.new_page()

                def on_request(request) -> None:
                    url = str(request.url)
                    append(
                        {
                            "kind": "request",
                            "url": url,
                            "method": str(request.method),
                            "resource_type": str(request.resource_type),
                            "post_data": (request.post_data or "")[:32768],
                            "noise": self._runtime_noise_url(url),
                        }
                    )

                def on_response(response) -> None:
                    url = str(response.url)
                    request = response.request
                    append(
                        {
                            "kind": "response",
                            "url": url,
                            "status": int(response.status),
                            "method": str(request.method),
                            "resource_type": str(request.resource_type),
                            "noise": self._runtime_noise_url(url),
                        }
                    )

                def on_websocket(ws) -> None:
                    url = str(ws.url)
                    noise = self._runtime_noise_url(url)
                    if not noise and url not in sockets:
                        sockets.append(url)
                    append(
                        {
                            "kind": "websocket_open",
                            "url": url,
                            "noise": noise,
                        }
                    )

                    def capture(direction: str):
                        def handler(payload) -> None:
                            append(
                                {
                                    "kind": "ws_frame",
                                    "direction": direction,
                                    "url": url,
                                    "noise": noise,
                                    "payload": self._runtime_payload_preview(payload),
                                }
                            )
                        return handler

                    ws.on("framesent", capture("sent"))
                    ws.on("framereceived", capture("received"))

                page.on("request", on_request)
                page.on("response", on_response)
                page.on("websocket", on_websocket)

                navigation_timeout_ms = max(3_000, int(min(timeout_s, 30.0) * 1000))
                page.goto(
                    demo_url,
                    wait_until="domcontentloaded",
                    timeout=navigation_timeout_ms,
                )

                # Let bootstrap/session traffic settle before changing phase.
                page.wait_for_timeout(2500)
                phase["value"] = "action"
                report["action"] = self._try_runtime_action(page)

                # Capture the immediate protocol delta caused by the diagnostic input.
                page.wait_for_timeout(3000)
                context.close()
                browser.close()
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"

        report["summary"] = self._runtime_signal_summary(events)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "runtime-activity.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return report

    def _discover_demo_protocol(
        self,
        game: Game,
        *,
        timeout_s: float,
        attempt_dir: Path,
    ) -> tuple[str, int, float, list[str], list[str]]:
        started = time.monotonic()
        session = self._worker_session()
        detail = session.get(game.url, timeout=timeout_s, allow_redirects=True)
        detail.raise_for_status()
        demo = self._resolve_demo_response(
            session,
            game,
            detail.text,
            timeout_s=timeout_s,
            attempt_dir=attempt_dir,
        )
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
                response = session.get(src, timeout=min(timeout_s, 20.0))
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

        runtime = self._capture_runtime_action(
            demo.url,
            timeout_s=timeout_s,
            attempt_dir=attempt_dir,
        )

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
                    "runtime_activity": {
                        "action": runtime.get("action") or {},
                        "summary": runtime.get("summary") or {},
                        "websocket_urls": runtime.get("websocket_urls") or [],
                        "error": runtime.get("error") or "",
                        "artifact": str(attempt_dir / "runtime-activity.json"),
                    },
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
        return demo.url, int(demo.status_code), elapsed_ms, scripts, endpoints

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
                demo_url, demo_status, elapsed_ms, _scripts, endpoints = self._discover_demo_protocol(
                    game,
                    timeout_s=timeout_s,
                    attempt_dir=attempt_dir,
                )
                bootstrap_ok += 1
                runtime_summary: dict[str, Any] = {}
                runtime_error = ""
                runtime_path = attempt_dir / "runtime-activity.json"
                if runtime_path.exists():
                    try:
                        runtime_doc = json.loads(runtime_path.read_text(encoding="utf-8"))
                        runtime_summary = dict(runtime_doc.get("summary") or {})
                        runtime_error = str(runtime_doc.get("error") or "")
                    except Exception:
                        runtime_summary = {}
                action_signals = int(runtime_summary.get("action_signals") or 0)
                progress(
                    f"[{game.name}] bootstrap {number}/{repetitions}: "
                    f"{elapsed_ms:.0f} ms, candidatos={len(endpoints)}, "
                    f"runtime_signals={action_signals}"
                    + (f", runtime_error={runtime_error}" if runtime_error else "")
                )
                warning = (
                    "Belatra runtime capturado; falta clasificar la firma del spin."
                    if action_signals
                    else "Belatra runtime capturado sin firma de acción concluyente."
                )
                attempts.append(
                    SpinAttempt(
                        number=number,
                        ok=True,
                        mode_id="BOOTSTRAP_DISCOVERY",
                        mode_kind="DISCOVERY",
                        status_code=demo_status,
                        elapsed_ms=elapsed_ms,
                        symbol=game.symbol or game.slug,
                        endpoint=demo_url,
                        na="",
                        terminal=False,
                        wire_steps=1,
                        warning=warning,
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
                        symbol=game.symbol or game.slug,
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
                "Demo Belatra accesible; se capturó tráfico runtime y se intentó una "
                "entrada de diagnóstico. Falta clasificar la firma de spin/bonus/buy "
                "antes de considerar la acción terminal."
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
            symbol=game.symbol or game.slug,
            discovered_modes=[
                {
                    "id": "RUNTIME_ACTION_DISCOVERY",
                    "kind": "DISCOVERY_RUNTIME",
                    "automated": True,
                    "runtime_capture": True,
                    "diagnostic_input": True,
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
