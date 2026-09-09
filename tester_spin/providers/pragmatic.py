from __future__ import annotations

import html
import json
import math
import mimetypes
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from playwright.sync_api import Response, sync_playwright

from tester_spin.models import Game, GameTestResult, SpinAttempt, utc_now_iso
from tester_spin.providers.base import Progress, ProviderAdapter
from tester_spin.providers.pragmatic_modes import PragmaticMode, PragmaticModeCatalog, discover_modes


DROP_HEADERS = {
    "content-length",
    "host",
    "connection",
    "accept-encoding",
    "cookie",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
}
FEATURE_STATE_FIELDS = ("rs_c", "rs_t", "rs_more", "fs", "fsmax", "puri", "purtr")
GAME_PATH_RE = re.compile(r"^/en/games/([^/?#]+)/?$", flags=re.I)


def _parse_wire(raw: bytes | str | None) -> dict[str, str]:
    if raw is None:
        return {}
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    text = text.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return {
                    str(k): json.dumps(v, ensure_ascii=False, separators=(",", ":"))
                    if isinstance(v, (dict, list))
                    else ""
                    if v is None
                    else str(v)
                    for k, v in obj.items()
                }
        except Exception:
            pass
    return {str(k): str(v) for k, v in parse_qsl(text, keep_blank_values=True)}


def _num(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def _fmt(value: float) -> str:
    if math.isclose(value, round(value), rel_tol=0.0, abs_tol=1e-12):
        return str(int(round(value)))
    return (f"{value:.12f}").rstrip("0").rstrip(".")


def _clean_blob(text: str) -> str:
    value = html.unescape(text or "")
    value = value.replace("\\/", "/")
    value = value.replace("\\u0026", "&").replace("\\u003d", "=")
    value = value.replace("\\x26", "&").replace("\\x3d", "=")
    return value


def _extract_launch_urls(text: str, base_url: str) -> list[str]:
    blob = _clean_blob(text)
    out: list[str] = []
    patterns = (
        r'https?://[^\s"\'<>]+(?:openGame|html5Game)\.do\?[^\s"\'<>]+',
        r'[/A-Za-z0-9._-]*/gs2c/(?:openGame|html5Game)\.do\?[^\s"\'<>]+',
    )
    for pattern in patterns:
        for match in re.findall(pattern, blob, flags=re.I):
            url = urljoin(base_url, html.unescape(match).rstrip(",);]"))
            if url not in out:
                out.append(url)
    return out


def _extract_mgckey(text: str) -> str | None:
    blob = _clean_blob(text)
    for candidate in (blob, unquote(blob)):
        match = re.search(r'(stylename@[^\s"\'&<>]*~SESSION@[A-Za-z0-9-]+)', candidate, flags=re.I)
        if match:
            return match.group(1)
        match = re.search(r'[?&]mgckey=([^&\s"\'<>]+)', candidate, flags=re.I)
        if match:
            return unquote(match.group(1))
    return None


def _extract_cver(text: str) -> str | None:
    blob = _clean_blob(unquote(text or ""))
    for pattern in (
        r'[?&]cver=(\d{3,})',
        r'["\']cver["\']\s*[:=]\s*["\']?(\d{3,})',
        r'\bcver\s*[:=]\s*["\']?(\d{3,})',
        r'clientVersion["\']?\s*[:=]\s*["\']?(\d{3,})',
    ):
        match = re.search(pattern, blob, flags=re.I)
        if match:
            return match.group(1)
    return None


def _extract_game_service(text: str) -> str | None:
    match = re.search(r'https?://[^\s"\'<>]+/gameService(?:\?[^\s"\'<>]*)?', _clean_blob(text), flags=re.I)
    return html.unescape(match.group(0)).rstrip(",);]") if match else None


def _human_from_slug(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


def _safe_human_folder(name: str) -> str:
    # Preserve the human game name on Windows; only characters forbidden by the
    # filesystem are replaced. Unicode, spaces and punctuation such as en-dash
    # are kept intact.
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name.strip())
    value = value.rstrip(" .")
    return value[:160] or "Unnamed Game"


def _srcset_best(srcset: str) -> str:
    best_url = ""
    best_score = -1.0
    for item in str(srcset or "").split(","):
        bits = item.strip().split()
        if not bits:
            continue
        url = bits[0]
        score = 1.0
        if len(bits) > 1:
            descriptor = bits[-1].lower()
            try:
                if descriptor.endswith("w"):
                    score = float(descriptor[:-1])
                elif descriptor.endswith("x"):
                    score = float(descriptor[:-1]) * 10_000.0
            except ValueError:
                score = 1.0
        if score >= best_score:
            best_url = url
            best_score = score
    return best_url


def _image_url(img: Tag | None, base_url: str) -> str:
    if img is None:
        return ""
    candidates = [
        _srcset_best(str(img.get("srcset") or "")),
        _srcset_best(str(img.get("data-srcset") or "")),
        str(img.get("data-original") or ""),
        str(img.get("data-lazy-src") or ""),
        str(img.get("data-src") or ""),
        str(img.get("src") or ""),
    ]
    for candidate in candidates:
        candidate = html.unescape(candidate.strip())
        if candidate and not candidate.startswith("data:"):
            return urljoin(base_url, candidate)
    return ""


@dataclass(slots=True)
class BrowserBootstrap:
    symbol: str
    mgckey: str
    cver: str | None
    endpoint: str
    launch_url: str
    headers: dict[str, str]
    cookies: list[dict[str, Any]]
    init_request_raw: str
    init_response_raw: bytes
    init_response: dict[str, str]
    calibration_request_raw: str
    calibration_response_raw: bytes
    calibration_response: dict[str, str]


@dataclass(slots=True)
class HttpBootstrap:
    session: requests.Session
    symbol: str
    mgckey: str
    cver: str | None
    endpoint: str
    launch_url: str
    spin_template: dict[str, str]
    init_request_raw: str
    init_response_raw: bytes
    init_response: dict[str, str]
    calibration_request_raw: str
    calibration_response_raw: bytes
    calibration_response: dict[str, str]


class PragmaticProvider(ProviderAdapter):
    key = "pragmatic"
    display_name = "Pragmatic Play"
    catalog_url = "https://www.pragmaticplay.com/en/games/"
    # Pragmatic's catalogue is large and a 40% drop is not a plausible normal
    # update. Fail closed unless an authoritative crawl retains at least 90% of
    # the previously known rows.
    min_catalog_reconcile_ratio = 0.90

    def __init__(self, data_root: Path, base_bet: float = 2.0) -> None:
        self.data_root = data_root
        self.provider_root = data_root / "providers" / self.key
        self.provider_root.mkdir(parents=True, exist_ok=True)
        self.base_bet = float(base_bet)
        self.http = requests.Session()
        self.http.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            }
        )

    def game_dir(self, game: Game) -> Path:
        return self.provider_root / _safe_human_folder(game.name)

    def catalog_record_invalid_reason(self, game: Game) -> str:
        slug = str(game.slug or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,100}", slug):
            return "slug Pragmatic malformado"

        parsed = urlparse(str(game.url or ""))
        if (parsed.hostname or "").casefold() not in {
            "pragmaticplay.com",
            "www.pragmaticplay.com",
        }:
            return "host de ficha no pertenece a Pragmatic"
        match = GAME_PATH_RE.match(parsed.path)
        if not match or match.group(1).strip().lower() != slug:
            return "URL de ficha no coincide con el slug"

        thumb = str(game.thumbnail_url or "").strip()
        if thumb.casefold().startswith("data:"):
            return "thumbnail data URI: no es una tarjeta de juego"

        return ""

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
    ) -> list[Game]:
        by_slug: dict[str, Game] = {}
        no_new_pages = 0
        for page_no in range(1, max(1, int(max_pages)) + 1):
            if stop_event.is_set():
                break
            page_url = self.catalog_url if page_no == 1 else urljoin(self.catalog_url, f"page/{page_no}/")
            progress(f"Catálogo Pragmatic: página {page_no} — {page_url}")
            response = self.http.get(page_url, timeout=30.0)
            if response.status_code == 404:
                break
            response.raise_for_status()
            page_games = self._extract_catalog_page(response.text, response.url)
            new_count = 0
            for game in page_games:
                if game.slug not in by_slug:
                    by_slug[game.slug] = game
                    new_count += 1
                    try:
                        self._persist_catalog_artifacts(game, response.url)
                    except Exception as exc:
                        progress(f"[{game.name}] miniatura/metadata: {type(exc).__name__}: {exc}")
                    progress(f"  + {game.name} — {game.url}")
                else:
                    current = by_slug[game.slug]
                    if not current.thumbnail_url and game.thumbnail_url:
                        current.thumbnail_url = game.thumbnail_url

            progress(
                f"Página {page_no}: {len(page_games)} enlaces de juego, "
                f"{new_count} nuevos, total={len(by_slug)}"
            )
            if new_count == 0:
                no_new_pages += 1
            else:
                no_new_pages = 0
            # WordPress/CDN may repeat the final page instead of 404. Two fully
            # duplicate pages are enough to stop without truncating on one anomaly.
            if no_new_pages >= 2:
                break

        games = sorted(by_slug.values(), key=lambda item: item.name.casefold())
        self._write_catalog_index(games)
        progress(f"Catálogo Pragmatic terminado: {len(games)} juegos únicos")
        return games

    def _extract_catalog_page(self, text: str, base_url: str) -> list[Game]:
        soup = BeautifulSoup(text, "html.parser")
        found: dict[str, Game] = {}
        for anchor in soup.find_all("a", href=True):
            href = urljoin(base_url, str(anchor.get("href") or ""))
            parsed = urlparse(href)
            match = GAME_PATH_RE.match(parsed.path)
            if not match:
                continue
            slug = match.group(1).strip().lower()
            if not slug:
                continue

            card = self._nearest_game_card(anchor)
            image = None
            if isinstance(card, Tag):
                image = card.find("img")
            if image is None:
                image = anchor.find("img")

            name = self._extract_human_name(anchor, card, image, slug)
            thumb = _image_url(image, base_url)
            canonical = f"https://www.pragmaticplay.com/en/games/{slug}/"

            existing = found.get(slug)
            if existing is None:
                found[slug] = Game(
                    provider=self.key,
                    slug=slug,
                    name=name,
                    url=canonical,
                    thumbnail_url=thumb,
                )
            else:
                if existing.name == _human_from_slug(slug) and name != existing.name:
                    existing.name = name
                if not existing.thumbnail_url and thumb:
                    existing.thumbnail_url = thumb
        return list(found.values())

    @staticmethod
    def _nearest_game_card(anchor: Tag) -> Tag:
        current: Tag = anchor
        for _ in range(7):
            parent = current.parent
            if not isinstance(parent, Tag):
                break
            current = parent
            image = current.find("img")
            if image is None:
                continue
            game_links: set[str] = set()
            for link in current.find_all("a", href=True):
                parsed = urlparse(str(link.get("href") or ""))
                match = GAME_PATH_RE.match(parsed.path)
                if match:
                    game_links.add(match.group(1).lower())
            if 1 <= len(game_links) <= 2:
                return current
        return anchor

    @staticmethod
    def _extract_human_name(anchor: Tag, card: Tag, image: Tag | None, slug: str) -> str:
        candidates: list[str] = []
        if image is not None:
            candidates.extend([str(image.get("alt") or ""), str(image.get("title") or "")])
        candidates.extend([str(anchor.get("aria-label") or ""), str(anchor.get("title") or "")])
        anchor_text = " ".join(anchor.stripped_strings)
        if anchor_text:
            candidates.append(anchor_text)
        if isinstance(card, Tag):
            for selector in ("h1", "h2", "h3", "h4", "h5", ".title", ".game-title", ".card-title"):
                try:
                    node = card.select_one(selector)
                except Exception:
                    node = None
                if node is not None:
                    candidates.append(" ".join(node.stripped_strings))

        ignored = {"", "play now", "play demo", "image", "learn more", "read more"}
        for candidate in candidates:
            cleaned = re.sub(r"\s+", " ", html.unescape(candidate)).strip(" \t\r\n-|–—")
            if cleaned.casefold() in ignored:
                continue
            if cleaned and len(cleaned) <= 120:
                return cleaned
        return _human_from_slug(slug)

    def _persist_catalog_artifacts(self, game: Game, catalog_page: str) -> None:
        root = self.game_dir(game)
        root.mkdir(parents=True, exist_ok=True)
        if game.thumbnail_url:
            game.thumbnail_path = self._download_native_thumbnail(game, root)
        metadata = self._read_json(root / "game.json")
        metadata.update(
            {
                "schema": "tester-spin/game/v1",
                "provider": self.display_name,
                "provider_key": self.key,
                "human_name": game.name,
                "provider_slug": game.slug,
                "provider_internal_id": metadata.get("provider_internal_id", game.symbol or ""),
                "catalog_page": catalog_page,
                "page_url": game.url,
                "direct_game_link": metadata.get("direct_game_link", ""),
                "thumbnail": {
                    "source_url": game.thumbnail_url,
                    "local_file": Path(game.thumbnail_path).name if game.thumbnail_path else "",
                    "policy": "original bytes from highest/native image URL exposed by catalog; no resize/re-encode",
                },
                "discovered_at": metadata.get("discovered_at", game.discovered_at),
                "catalog_recovery_disabled": False,
                "updated_at": utc_now_iso(),
            }
        )
        self._write_json(root / "game.json", metadata)

    def _download_native_thumbnail(self, game: Game, root: Path) -> str:
        response = self.http.get(game.thumbnail_url, timeout=30.0, stream=True, headers={"Referer": game.url})
        response.raise_for_status()
        parsed = urlparse(game.thumbnail_url)
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
            suffix = mimetypes.guess_extension(content_type) or ".img"
            if suffix == ".jpe":
                suffix = ".jpg"
        path = root / f"thumbnail{suffix}"
        tmp = root / f"thumbnail{suffix}.tmp"
        with tmp.open("wb") as handle:
            for chunk in response.iter_content(256 * 1024):
                if chunk:
                    handle.write(chunk)
        tmp.replace(path)
        return str(path)

    def _recover_catalog_games_from_artifacts(self) -> list[Game]:
        """Recover previously discovered games from preserved per-game metadata.

        This is used only when the live catalogue crawl is non-authoritative. It
        keeps a temporary site/WAF failure from making the GUI forget hundreds of
        known games. A later authoritative crawl is still responsible for removing
        genuinely retired titles.
        """
        recovered: dict[str, Game] = {}
        if not self.provider_root.exists():
            return []

        for child in self.provider_root.iterdir():
            if not child.is_dir():
                continue
            metadata = self._read_json(child / "game.json")
            if not isinstance(metadata, dict):
                continue
            if str(metadata.get("provider_key") or "").strip() != self.key:
                continue
            if bool(metadata.get("catalog_recovery_disabled")):
                continue

            slug = str(metadata.get("provider_slug") or "").strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,100}", slug):
                continue

            page_url = str(metadata.get("page_url") or "").strip()
            if not page_url:
                page_url = f"https://www.pragmaticplay.com/en/games/{slug}/"
            parsed = urlparse(page_url)
            if (parsed.hostname or "").casefold() not in {
                "pragmaticplay.com",
                "www.pragmaticplay.com",
            }:
                continue
            if not GAME_PATH_RE.match(parsed.path):
                continue

            thumb_meta = metadata.get("thumbnail") or {}
            thumb_url = ""
            local_file = ""
            if isinstance(thumb_meta, dict):
                thumb_url = str(thumb_meta.get("source_url") or "").strip()
                local_file = str(thumb_meta.get("local_file") or "").strip()

            symbol = str(metadata.get("provider_internal_id") or "").strip()

            # Reject artifacts that look like site chrome accidentally captured by
            # an old DOM fallback. Real catalog thumbnails are normal HTTP assets;
            # a data URI such as the language-icon false positive is never valid.
            if thumb_url.casefold().startswith("data:"):
                continue
            if thumb_url:
                thumb_path = unquote(urlparse(thumb_url).path)
                looks_native_thumb = (
                    "/wp-content/uploads/" in thumb_path.casefold()
                    and re.search(r"[_-]\d{2,4}x\d{2,4}(?:[_-]|\.|$)", Path(thumb_path).name, re.I)
                )
                if not looks_native_thumb and not symbol:
                    continue
            elif not symbol:
                continue

            local_path = ""
            if local_file:
                candidate = child / local_file
                if candidate.exists():
                    local_path = str(candidate)

            name = str(metadata.get("human_name") or "").strip() or _human_from_slug(slug)
            recovered[slug] = Game(
                provider=self.key,
                slug=slug,
                name=name,
                url=f"https://www.pragmaticplay.com/en/games/{slug}/",
                thumbnail_url=thumb_url,
                thumbnail_path=local_path,
                symbol=symbol,
            )

        return sorted(recovered.values(), key=lambda item: item.name.casefold())

    def _write_catalog_index(self, games: list[Game]) -> None:
        payload = {
            "schema": "tester-spin/provider-catalog/v1",
            "provider": self.display_name,
            "provider_key": self.key,
            "source_url": self.catalog_url,
            "updated_at": utc_now_iso(),
            "count": len(games),
            "games": [
                {
                    "name": game.name,
                    "slug": game.slug,
                    "url": game.url,
                    "thumbnail_url": game.thumbnail_url,
                    "folder": self.game_dir(game).name,
                }
                for game in games
            ],
        }
        self._write_json(self.provider_root / "catalog.json", payload)

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        started_wall = time.monotonic()
        started_at = utc_now_iso()
        game_root = self.game_dir(game)
        game_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        run_root = game_root / "tests" / stamp
        run_root.mkdir(parents=True, exist_ok=True)

        attempts: list[SpinAttempt] = []
        symbol = game.symbol
        catalog: PragmaticModeCatalog | None = None
        overall_error = ""

        try:
            discovery: BrowserBootstrap
            bootstrap_timeout = max(60.0, timeout_s)

            if symbol:
                progress(
                    f"Usando ID interno conocido del catálogo: symbol={symbol}; "
                    "bootstrap directo sin depender de la ficha pública."
                )
                known_bootstrap: HttpBootstrap | None = None
                try:
                    known_bootstrap = self._http_bootstrap(
                        game.url,
                        symbol,
                        None,
                        self.base_bet,
                        bootstrap_timeout,
                    )
                    cookies = [
                        {
                            "name": cookie.name,
                            "value": cookie.value,
                            "domain": cookie.domain,
                            "path": cookie.path,
                        }
                        for cookie in known_bootstrap.session.cookies
                    ]
                    headers = {
                        str(key): str(value)
                        for key, value in known_bootstrap.session.headers.items()
                    }
                    discovery = BrowserBootstrap(
                        symbol=known_bootstrap.symbol,
                        mgckey=known_bootstrap.mgckey,
                        cver=known_bootstrap.cver,
                        endpoint=known_bootstrap.endpoint,
                        launch_url=known_bootstrap.launch_url,
                        headers=headers,
                        cookies=cookies,
                        init_request_raw=known_bootstrap.init_request_raw,
                        init_response_raw=known_bootstrap.init_response_raw,
                        init_response=dict(known_bootstrap.init_response),
                        calibration_request_raw=known_bootstrap.calibration_request_raw,
                        calibration_response_raw=known_bootstrap.calibration_response_raw,
                        calibration_response=dict(known_bootstrap.calibration_response),
                    )
                    progress(f"ID conocido validado directamente: symbol={discovery.symbol}")
                except Exception as known_exc:
                    progress(
                        "El ID conocido no pudo bootstrappear "
                        f"({type(known_exc).__name__}: {known_exc}); "
                        "se intenta resolver nuevamente desde la ficha pública."
                    )
                    discovery = self._browser_bootstrap(
                        game.url,
                        timeout_s=bootstrap_timeout,
                        progress=progress,
                    )
                finally:
                    if known_bootstrap is not None:
                        known_bootstrap.session.close()
            else:
                progress("Descubriendo ID interno y protocolo con el cliente oficial...")
                discovery = self._browser_bootstrap(
                    game.url,
                    timeout_s=bootstrap_timeout,
                    progress=progress,
                )

            symbol = discovery.symbol
            game.symbol = symbol
            catalog = discover_modes(discovery.init_response, requested_base_bet=self.base_bet)
            self._write_discovery(run_root, discovery, catalog)
            self._update_game_protocol_metadata(game, discovery, catalog)

            modes = catalog.enabled()
            progress(
                "Modos detectados: "
                + ", ".join(
                    f"{mode.id}(x{mode.price_x_base:g})" if mode.price_known else f"{mode.id}(precio desconocido)"
                    for mode in modes
                )
            )

            attempt_number = 0
            repetitions = max(1, int(spins))
            for mode in modes:
                for repetition in range(1, repetitions + 1):
                    if stop_event.is_set():
                        raise InterruptedError("Prueba detenida por el usuario")
                    attempt_number += 1
                    progress(f"{mode.id}: prueba {repetition}/{repetitions}")
                    attempt = self._test_mode_once(
                        game,
                        symbol=symbol,
                        cver=discovery.cver,
                        mode=mode,
                        catalog=catalog,
                        attempt_number=attempt_number,
                        repetition=repetition,
                        run_root=run_root,
                        timeout_s=timeout_s,
                    )
                    attempts.append(attempt)
                    if attempt.ok:
                        suffix = f"; {attempt.warning}" if attempt.warning else ""
                        progress(
                            f"{mode.id}: respuesta OK HTTP {attempt.status_code}, "
                            f"na={attempt.na!r}, steps={attempt.wire_steps}{suffix}"
                        )
                    else:
                        progress(f"{mode.id}: ERROR {attempt.error}")
        except InterruptedError as exc:
            overall_error = str(exc)
        except Exception as exc:
            overall_error = f"{type(exc).__name__}: {exc}"
            progress(f"ERROR de juego: {overall_error}")

        successful = sum(1 for attempt in attempts if attempt.ok)
        failed = sum(1 for attempt in attempts if not attempt.ok)
        if overall_error and not attempts:
            status = "ERROR"
        elif overall_error:
            status = "PARCIAL"
        elif failed == 0:
            status = "OK"
        elif successful:
            status = "PARCIAL"
        else:
            status = "ERROR"

        requested = len(catalog.enabled()) * max(1, int(spins)) if catalog is not None else max(1, int(spins))
        finished_at = utc_now_iso()
        elapsed_ms = (time.monotonic() - started_wall) * 1000.0
        result = GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=requested,
            successful_spins=successful,
            failed_spins=max(failed, requested - successful if overall_error else failed),
            status=status,
            symbol=symbol,
            discovered_modes=[] if catalog is None else [mode.to_dict() for mode in catalog.enabled()],
            started_at=started_at,
            finished_at=finished_at,
            elapsed_ms=elapsed_ms,
            error=overall_error,
            run_dir=str(run_root),
            attempts=attempts,
        )
        self._write_json(run_root / "result.json", result.to_dict())
        self._record_last_test_in_game_json(game, result)
        return result

    def _browser_bootstrap(self, source_url: str, timeout_s: float, progress: Progress) -> BrowserBootstrap:
        init_exchange: dict[str, Any] = {}
        spin_exchange: dict[str, Any] = {}
        launch_url = ""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--mute-audio", "--disable-background-timer-throttling"],
            )
            context = browser.new_context(viewport={"width": 1280, "height": 720}, locale="en-US")

            def on_response(response: Response) -> None:
                nonlocal launch_url
                request = response.request
                if "openGame.do" in request.url or "html5Game.do" in request.url:
                    launch_url = request.url
                if request.resource_type not in {"xhr", "fetch", "document"}:
                    return
                raw_request = request.post_data or urlparse(request.url).query
                fields = _parse_wire(raw_request)
                action = str(fields.get("action") or "")
                if action not in {"doInit", "doSpin"}:
                    return
                try:
                    raw_response = response.body()
                except Exception:
                    return
                item = {
                    "url": request.url,
                    "request_raw": raw_request,
                    "request": fields,
                    "response_raw": raw_response,
                    "response": _parse_wire(raw_response),
                    "status": response.status,
                    "headers": {k: v for k, v in request.headers.items() if k.lower() not in DROP_HEADERS},
                }
                if action == "doInit" and not init_exchange:
                    init_exchange.update(item)
                elif action == "doSpin" and not spin_exchange:
                    spin_exchange.update(item)

            context.on("response", on_response)
            page = context.new_page()
            page.goto(source_url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
            deadline = time.monotonic() + timeout_s
            gate_words = (
                "Accept All",
                "Aceptar todo",
                "Play Demo",
                "Jugar demo",
                "I am 18",
                "Tengo 18",
            )
            while time.monotonic() < deadline and not init_exchange:
                for frame in page.frames:
                    for text in gate_words:
                        try:
                            locator = frame.get_by_text(text, exact=False)
                            if locator.count() and locator.first.is_visible():
                                locator.first.click(timeout=250)
                        except Exception:
                            pass
                page.wait_for_timeout(200)
            if not init_exchange:
                context.close()
                browser.close()
                raise RuntimeError("No se capturó doInit")

            progress("doInit capturado; obteniendo doSpin de calibración")
            while time.monotonic() < deadline and not spin_exchange:
                try:
                    page.keyboard.press("Space")
                except Exception:
                    pass
                page.wait_for_timeout(250)
            if not spin_exchange:
                context.close()
                browser.close()
                raise RuntimeError("No se capturó doSpin de calibración")
            cookies = context.cookies()
            context.close()
            browser.close()

        spin_request = spin_exchange["request"]
        init_request = init_exchange["request"]
        symbol = str(spin_request.get("symbol") or init_request.get("symbol") or "")
        mgckey = str(spin_request.get("mgckey") or init_request.get("mgckey") or "")
        cver = str(init_request.get("cver") or spin_request.get("cver") or "") or None
        if not symbol or not mgckey:
            raise RuntimeError("Bootstrap sin symbol/mgckey")
        return BrowserBootstrap(
            symbol=symbol,
            mgckey=mgckey,
            cver=cver,
            endpoint=str(spin_exchange["url"]),
            launch_url=launch_url or str(spin_exchange.get("headers", {}).get("referer") or ""),
            headers=dict(spin_exchange.get("headers") or {}),
            cookies=list(cookies),
            init_request_raw=str(init_exchange["request_raw"]),
            init_response_raw=bytes(init_exchange["response_raw"]),
            init_response=dict(init_exchange["response"]),
            calibration_request_raw=str(spin_exchange["request_raw"]),
            calibration_response_raw=bytes(spin_exchange["response_raw"]),
            calibration_response=dict(spin_exchange["response"]),
        )

    def _http_bootstrap(
        self,
        source_url: str,
        symbol: str,
        cver: str | None,
        base_bet: float,
        timeout_s: float,
    ) -> HttpBootstrap:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            }
        )
        demo_base = "https://demogamesfree.pragmaticplay.net"
        open_game_url = demo_base + "/gs2c/openGame.do?" + urlencode(
            {
                "gameSymbol": symbol,
                "websiteUrl": demo_base,
                "jurisdiction": "99",
                "lobby_url": source_url,
                "lang": "en",
                "cur": "USD",
            }
        )
        launch = session.get(open_game_url, allow_redirects=True, timeout=timeout_s, headers={"Referer": source_url})
        launch.raise_for_status()
        launch_text = launch.text
        final_url = launch.url
        mgckey = _extract_mgckey(final_url) or _extract_mgckey(launch_text)
        if not mgckey:
            for nested_url in _extract_launch_urls(launch_text, final_url)[:5]:
                nested = session.get(nested_url, allow_redirects=True, timeout=timeout_s, headers={"Referer": final_url})
                if nested.status_code >= 400:
                    continue
                final_url = nested.url
                launch_text += "\n" + nested.text
                mgckey = _extract_mgckey(final_url) or _extract_mgckey(nested.text)
                if mgckey:
                    break
        if not mgckey:
            session.close()
            raise RuntimeError("HTTP bootstrap no resolvió mgckey")

        resolved_cver = cver or _extract_cver(final_url) or _extract_cver(launch_text)
        endpoint = _extract_game_service(launch_text)
        if not endpoint:
            parsed = urlparse(final_url)
            service_path = "/gs2c/ge/v5/gameService" if "/gs2c/" in parsed.path else "/hub-demo/ge/v5/gameService"
            endpoint = f"{parsed.scheme}://{parsed.netloc}{service_path}"
        origin = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}"
        headers = {
            "User-Agent": session.headers["User-Agent"],
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
            "Referer": final_url,
        }

        init_fields = {
            "action": "doInit",
            "symbol": symbol,
            "index": "1",
            "counter": "1",
            "repeat": "0",
            "mgckey": mgckey,
        }
        if resolved_cver:
            init_fields["cver"] = resolved_cver
        init_raw = urlencode(init_fields)
        init_response = session.post(endpoint, data=init_raw, headers=headers, timeout=timeout_s)
        init_response.raise_for_status()
        init = _parse_wire(init_response.content)
        catalog = discover_modes(init, requested_base_bet=base_bet)
        next_index = (_int(init.get("index")) or 1) + 1
        next_counter = (_int(init.get("counter")) or 1) + 1
        spin_fields = {
            "action": "doSpin",
            "symbol": symbol,
            "c": _fmt(catalog.base_coin),
            "l": _fmt(catalog.base_scale),
            "sInfo": "t",
            "bl": "0",
            "index": str(next_index),
            "counter": str(next_counter),
            "repeat": "0",
            "mgckey": mgckey,
        }
        calibration_raw = urlencode(spin_fields)
        calibration_response = session.post(endpoint, data=calibration_raw, headers=headers, timeout=timeout_s)
        calibration_response.raise_for_status()
        return HttpBootstrap(
            session=session,
            symbol=symbol,
            mgckey=mgckey,
            cver=resolved_cver,
            endpoint=endpoint,
            launch_url=final_url,
            spin_template=spin_fields,
            init_request_raw=init_raw,
            init_response_raw=init_response.content,
            init_response=init,
            calibration_request_raw=calibration_raw,
            calibration_response_raw=calibration_response.content,
            calibration_response=_parse_wire(calibration_response.content),
        )

    def _test_mode_once(
        self,
        game: Game,
        *,
        symbol: str,
        cver: str | None,
        mode: PragmaticMode,
        catalog: PragmaticModeCatalog,
        attempt_number: int,
        repetition: int,
        run_root: Path,
        timeout_s: float,
    ) -> SpinAttempt:
        attempt_root = run_root / mode.id / f"attempt-{repetition:04d}"
        attempt_root.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        bootstrap: HttpBootstrap | None = None
        try:
            bootstrap = self._http_bootstrap(game.url, symbol, cver, catalog.base_bet, timeout_s)
            self._write_http_bootstrap(attempt_root, bootstrap)
            index = (_int(bootstrap.calibration_response.get("index")) or _int(bootstrap.init_response.get("index")) or 1) + 1
            counter = (_int(bootstrap.calibration_response.get("counter")) or _int(bootstrap.init_response.get("counter")) or 1) + 1

            fields = dict(bootstrap.spin_template)
            fields.update(
                {
                    "action": "doSpin",
                    "symbol": symbol,
                    "c": _fmt(catalog.base_coin),
                    "l": _fmt(catalog.base_scale),
                    "bl": str(mode.provider_bl or 0),
                    "index": str(index),
                    "counter": str(counter),
                    "repeat": fields.get("repeat", "0") or "0",
                    "mgckey": bootstrap.mgckey,
                }
            )
            if mode.kind == "PURCHASE":
                fields["pur"] = str(mode.provider_pur)
            else:
                fields.pop("pur", None)

            status_code, response_raw, response_fields, headers = self._post_and_store(
                bootstrap,
                fields,
                attempt_root,
                step=0,
                label="entry",
                timeout_s=timeout_s,
            )
            if status_code >= 400:
                raise RuntimeError(f"HTTP {status_code}")
            server_error = response_fields.get("error") or response_fields.get("err") or response_fields.get("errorCode")
            if server_error not in (None, "", "0"):
                raise RuntimeError(f"server error={server_error}")

            last = response_fields
            wire_steps = 1
            terminal = False
            warning = ""
            while wire_steps < 128:
                na = str(last.get("na") or "")
                if na == "c":
                    index = (_int(last.get("index")) or index) + 1
                    counter = (_int(last.get("counter")) or counter) + 1
                    collect_fields = {
                        "symbol": symbol,
                        "action": "doCollect",
                        "index": str(index),
                        "counter": str(counter),
                        "repeat": "0",
                        "mgckey": bootstrap.mgckey,
                    }
                    status_code, _, last, _ = self._post_and_store(
                        bootstrap,
                        collect_fields,
                        attempt_root,
                        step=wire_steps,
                        label="collect",
                        timeout_s=timeout_s,
                    )
                    wire_steps += 1
                    terminal = status_code < 400
                    break

                if na == "s" and self._feature_active(last):
                    index = (_int(last.get("index")) or index) + 1
                    counter = (_int(last.get("counter")) or counter) + 1
                    continuation = dict(bootstrap.spin_template)
                    continuation.update(
                        {
                            "action": "doSpin",
                            "symbol": symbol,
                            "c": _fmt(catalog.base_coin),
                            "l": _fmt(catalog.base_scale),
                            "bl": str(mode.provider_bl or 0),
                            "index": str(index),
                            "counter": str(counter),
                            "repeat": "0",
                            "mgckey": bootstrap.mgckey,
                        }
                    )
                    continuation.pop("pur", None)
                    status_code, _, last, _ = self._post_and_store(
                        bootstrap,
                        continuation,
                        attempt_root,
                        step=wire_steps,
                        label="continuation-spin",
                        timeout_s=timeout_s,
                    )
                    wire_steps += 1
                    if status_code >= 400:
                        break
                    continue

                if na in {"", "s"}:
                    terminal = True
                    break

                # Unknown provider state (for example na=b observed on some buy
                # features) is evidence, not a discarded failure. The exact RAW
                # response is already on disk so a future handler can be added.
                warning = f"estado de continuación no automatizado: na={na!r}; RAW preservado"
                break

            elapsed_ms = (time.monotonic() - started) * 1000.0
            attempt = SpinAttempt(
                number=attempt_number,
                ok=True,
                mode_id=mode.id,
                mode_kind=mode.kind,
                provider_bl=mode.provider_bl,
                provider_pur=mode.provider_pur,
                status_code=status_code,
                elapsed_ms=elapsed_ms,
                symbol=symbol,
                endpoint=bootstrap.endpoint,
                na=str(last.get("na") or ""),
                terminal=terminal,
                wire_steps=wire_steps,
                warning=warning,
                artifact_dir=str(attempt_root),
            )
            self._write_json(attempt_root / "attempt.json", attempt.to_dict())
            return attempt
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            attempt = SpinAttempt(
                number=attempt_number,
                ok=False,
                mode_id=mode.id,
                mode_kind=mode.kind,
                provider_bl=mode.provider_bl,
                provider_pur=mode.provider_pur,
                elapsed_ms=elapsed_ms,
                symbol=symbol,
                endpoint="" if bootstrap is None else bootstrap.endpoint,
                error=f"{type(exc).__name__}: {exc}",
                artifact_dir=str(attempt_root),
            )
            self._write_json(attempt_root / "attempt.json", attempt.to_dict())
            return attempt
        finally:
            if bootstrap is not None:
                bootstrap.session.close()

    def _post_and_store(
        self,
        bootstrap: HttpBootstrap,
        fields: dict[str, str],
        root: Path,
        *,
        step: int,
        label: str,
        timeout_s: float,
    ) -> tuple[int, bytes, dict[str, str], dict[str, str]]:
        raw_request = urlencode(fields)
        started = time.time()
        response = bootstrap.session.post(
            bootstrap.endpoint,
            data=raw_request,
            timeout=timeout_s,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        elapsed_ms = (time.time() - started) * 1000.0
        raw_response = response.content
        parsed = _parse_wire(raw_response)
        prefix = f"step-{step:03d}-{label}"
        (root / f"{prefix}.request.txt").write_text(raw_request, encoding="utf-8")
        (root / f"{prefix}.response.raw").write_bytes(raw_response)
        self._write_json(root / f"{prefix}.response.json", parsed)
        self._write_json(
            root / f"{prefix}.http.json",
            {
                "status": response.status_code,
                "elapsed_ms": elapsed_ms,
                "request_url": response.request.url,
                "request_headers": dict(response.request.headers),
                "response_headers": dict(response.headers),
            },
        )
        return response.status_code, raw_response, parsed, dict(response.headers)

    @staticmethod
    def _feature_active(response: dict[str, str]) -> bool:
        return any(response.get(key) not in (None, "") for key in FEATURE_STATE_FIELDS)

    def _write_discovery(
        self,
        run_root: Path,
        discovery: BrowserBootstrap,
        catalog: PragmaticModeCatalog,
    ) -> None:
        root = run_root / "discovery"
        root.mkdir(parents=True, exist_ok=True)
        (root / "doInit.request.txt").write_text(discovery.init_request_raw, encoding="utf-8")
        (root / "doInit.response.raw").write_bytes(discovery.init_response_raw)
        self._write_json(root / "doInit.response.json", discovery.init_response)
        (root / "calibration.request.txt").write_text(discovery.calibration_request_raw, encoding="utf-8")
        (root / "calibration.response.raw").write_bytes(discovery.calibration_response_raw)
        self._write_json(root / "calibration.response.json", discovery.calibration_response)
        self._write_json(root / "modes.json", catalog.to_dict())
        self._write_json(
            root / "protocol.json",
            {
                "symbol": discovery.symbol,
                "cver": discovery.cver,
                "endpoint": discovery.endpoint,
                "direct_game_link": discovery.launch_url,
                "request_headers": discovery.headers,
                "cookies_present": [cookie.get("name") for cookie in discovery.cookies],
            },
        )

    def _write_http_bootstrap(self, root: Path, bootstrap: HttpBootstrap) -> None:
        boot = root / "bootstrap"
        boot.mkdir(parents=True, exist_ok=True)
        (boot / "doInit.request.txt").write_text(bootstrap.init_request_raw, encoding="utf-8")
        (boot / "doInit.response.raw").write_bytes(bootstrap.init_response_raw)
        self._write_json(boot / "doInit.response.json", bootstrap.init_response)
        (boot / "calibration.request.txt").write_text(bootstrap.calibration_request_raw, encoding="utf-8")
        (boot / "calibration.response.raw").write_bytes(bootstrap.calibration_response_raw)
        self._write_json(boot / "calibration.response.json", bootstrap.calibration_response)
        self._write_json(
            boot / "protocol.json",
            {
                "symbol": bootstrap.symbol,
                "cver": bootstrap.cver,
                "endpoint": bootstrap.endpoint,
                "direct_game_link": bootstrap.launch_url,
            },
        )

    def _update_game_protocol_metadata(
        self,
        game: Game,
        discovery: BrowserBootstrap,
        catalog: PragmaticModeCatalog,
    ) -> None:
        root = self.game_dir(game)
        metadata = self._read_json(root / "game.json")
        metadata.update(
            {
                "schema": "tester-spin/game/v1",
                "provider": self.display_name,
                "provider_key": self.key,
                "human_name": game.name,
                "provider_slug": game.slug,
                "provider_internal_id": discovery.symbol,
                "page_url": game.url,
                "direct_game_link": discovery.launch_url,
                "provider_protocol": {
                    "symbol": discovery.symbol,
                    "cver": discovery.cver,
                    "endpoint": discovery.endpoint,
                    "mode_catalog": catalog.to_dict(),
                    "doInit_fields": discovery.init_response,
                    "calibration_fields": discovery.calibration_response,
                },
                "updated_at": utc_now_iso(),
            }
        )
        self._write_json(root / "game.json", metadata)
        game.symbol = discovery.symbol

    def _record_last_test_in_game_json(self, game: Game, result: GameTestResult) -> None:
        root = self.game_dir(game)
        metadata = self._read_json(root / "game.json")
        metadata["last_test"] = {
            "status": result.status,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "elapsed_ms": result.elapsed_ms,
            "requested_mode_attempts": result.requested_spins,
            "successful_mode_attempts": result.successful_spins,
            "failed_mode_attempts": result.failed_spins,
            "run_dir": result.run_dir,
            "error": result.error,
        }
        metadata["updated_at"] = utc_now_iso()
        self._write_json(root / "game.json", metadata)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
