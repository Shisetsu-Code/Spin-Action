from __future__ import annotations

import html
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Response, sync_playwright

from tester_spin.models import Game, utc_now_iso
from tester_spin.providers.base import GameCallback, Progress
from tester_spin.providers.pragmatic import _human_from_slug


# Dynamic responses can contain full URLs, relative URLs, escaped HTML or JSON.
# _clean_dynamic_text() normalizes escaped slashes before this regex is applied.
_GAME_URL_RE = re.compile(
    r"(?P<url>(?:https?://[^\s\"'<>]+)?/(?:[a-z]{2}(?:-[a-z]{2})?/)?games/"
    r"(?P<slug>[a-z0-9][a-z0-9_-]{1,100})/?(?:[?#][^\s\"'<>]*)?)",
    re.I,
)

_RESERVED_SLUGS = {"page", "search", "category", "categories", "tag", "feed"}
_LOAD_MORE_RE = re.compile(r"(?:load\s+more\s+games|cargar\s+m[aá]s\s+juegos)", re.I)

_LINK_KEYS = {
    "url",
    "href",
    "link",
    "permalink",
    "gameurl",
    "game_url",
    "demourl",
    "demo_url",
}
_NAME_KEYS = {"name", "title", "game", "gamename", "game_name", "post_title"}
_IMAGE_KEYS = {"image", "imageurl", "image_url", "thumbnail", "thumbnailurl", "thumbnail_url", "thumb"}


def _clean_dynamic_text(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = text.replace("\\/", "/")
    text = text.replace("\\u002F", "/").replace("\\u002f", "/")
    text = text.replace("\\u003A", ":").replace("\\u003a", ":")
    text = text.replace("\\u0026", "&")
    return text


def _game_from_url(url: str, base_url: str, name: str = "", thumbnail_url: str = "") -> Game | None:
    cleaned = _clean_dynamic_text(url).strip().strip("\"'")
    if not cleaned:
        return None
    absolute = urljoin(base_url, cleaned)
    parsed = urlparse(absolute)
    path = parsed.path.replace("\\", "/")
    match = re.search(
        r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?games/([a-z0-9][a-z0-9_-]{1,100})/?$",
        path,
        re.I,
    )
    if not match:
        return None
    slug = match.group(1).strip().lower()
    if slug in _RESERVED_SLUGS:
        return None
    return Game(
        provider="pragmatic",
        slug=slug,
        name=(name or _human_from_slug(slug)).strip(),
        url=f"https://www.pragmaticplay.com/en/games/{slug}/",
        thumbnail_url=urljoin(base_url, thumbnail_url) if thumbnail_url else "",
    )


def _iter_json_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _iter_json_objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_json_objects(nested)


def _iter_strings(value: Any, *, limit: int = 10_000) -> Iterable[str]:
    emitted = 0
    stack = [value]
    while stack and emitted < limit:
        current = stack.pop()
        if isinstance(current, str):
            emitted += 1
            yield current
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def extract_dynamic_games(provider, text: str, base_url: str) -> list[Game]:
    """Extract games from full HTML, fragments, JSON and escaped payloads."""
    cleaned = _clean_dynamic_text(text)
    found: dict[str, Game] = {}

    def merge(game: Game | None) -> None:
        if game is None:
            return
        current = found.get(game.slug)
        if current is None:
            found[game.slug] = game
            return
        fallback_name = _human_from_slug(game.slug)
        if current.name == fallback_name and game.name and game.name != fallback_name:
            current.name = game.name
        if not current.thumbnail_url and game.thumbnail_url:
            current.thumbnail_url = game.thumbnail_url

    # Highest-quality path: reuse the existing HTML card parser.
    try:
        for game in provider._extract_catalog_page(cleaned, base_url):
            merge(game)
    except Exception:
        pass

    # Recovery path for arbitrary HTML/JSON/JS strings containing game URLs.
    for match in _GAME_URL_RE.finditer(cleaned):
        merge(_game_from_url(match.group("url"), base_url))

    parsed: Any = None
    stripped = cleaned.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except Exception:
            parsed = None

    if parsed is not None:
        for mapping in _iter_json_objects(parsed):
            lowered = {
                str(key).replace("-", "_").casefold(): value
                for key, value in mapping.items()
            }
            links: list[str] = []
            for key in _LINK_KEYS:
                value = lowered.get(key.casefold())
                if isinstance(value, str) and value:
                    links.append(value)
            if not links:
                continue

            human_name = ""
            for key in _NAME_KEYS:
                value = lowered.get(key.casefold())
                if isinstance(value, str) and value.strip():
                    human_name = re.sub(r"<[^>]+>", "", html.unescape(value)).strip()
                    break

            thumbnail = ""
            for key in _IMAGE_KEYS:
                value = lowered.get(key.casefold())
                if isinstance(value, str) and value.strip():
                    thumbnail = _clean_dynamic_text(value).strip()
                    break
                if isinstance(value, dict):
                    candidate = value.get("url") or value.get("src")
                    if isinstance(candidate, str):
                        thumbnail = _clean_dynamic_text(candidate).strip()
                        break

            for link in links:
                merge(_game_from_url(link, base_url, human_name, thumbnail))

        # JSON wrappers often carry an HTML fragment in a generic property.
        for string_value in _iter_strings(parsed):
            fragment = _clean_dynamic_text(string_value)
            if "/games/" not in fragment:
                continue
            try:
                for game in provider._extract_catalog_page(fragment, base_url):
                    merge(game)
            except Exception:
                pass
            for match in _GAME_URL_RE.finditer(fragment):
                merge(_game_from_url(match.group("url"), base_url))

    return list(found.values())


def _find_load_more(page):
    candidates = []
    for role in ("button", "link"):
        try:
            candidates.append(page.get_by_role(role, name=_LOAD_MORE_RE))
        except Exception:
            pass
    try:
        candidates.append(page.get_by_text(_LOAD_MORE_RE, exact=False))
    except Exception:
        pass
    try:
        candidates.append(
            page.locator("button,a,[role='button'],input[type='button'],input[type='submit']")
            .filter(has_text=_LOAD_MORE_RE)
        )
    except Exception:
        pass

    for locator in candidates:
        try:
            count = locator.count()
        except Exception:
            continue
        for index in range(min(count, 20)):
            item = locator.nth(index)
            try:
                if item.is_visible():
                    return item
            except Exception:
                continue

    try:
        handle = page.evaluate_handle(
            """() => {
                const re = /(load\\s+more\\s+games|cargar\\s+m[aá]s\\s+juegos)/i;
                const nodes = Array.from(document.querySelectorAll(
                    'button,a,[role="button"],input[type="button"],input[type="submit"]'
                ));
                return nodes.find(el => {
                    const label = (el.innerText || el.textContent || el.value ||
                                   el.getAttribute('aria-label') || '').trim();
                    return re.test(label) && el.getClientRects().length > 0;
                }) || null;
            }"""
        )
        element = handle.as_element()
        if element is not None:
            return element
    except Exception:
        pass
    return None


def _click_load_more(page, locator) -> str:
    try:
        locator.scroll_into_view_if_needed(timeout=4_000)
    except Exception:
        pass

    for strategy in ("normal", "force", "dom"):
        try:
            if strategy == "normal":
                locator.click(timeout=5_000)
            elif strategy == "force":
                locator.click(timeout=5_000, force=True)
            else:
                locator.evaluate(
                    """el => {
                        const target = el.closest('button,a,[role="button"]') || el;
                        target.scrollIntoView({block: 'center'});
                        target.click();
                    }"""
                )
            return strategy
        except Exception:
            continue

    clicked = page.evaluate(
        """() => {
            const re = /(load\\s+more\\s+games|cargar\\s+m[aá]s\\s+juegos)/i;
            const nodes = Array.from(document.querySelectorAll(
                'button,a,[role="button"],input[type="button"],input[type="submit"]'
            ));
            const el = nodes.find(node => {
                const label = (node.innerText || node.textContent || node.value ||
                               node.getAttribute('aria-label') || '').trim();
                return re.test(label) && node.getClientRects().length > 0;
            });
            if (!el) return false;
            el.scrollIntoView({block: 'center'});
            el.click();
            return true;
        }"""
    )
    if clicked:
        return "dom-search"
    raise RuntimeError("no se pudo activar Load More Games")


def crawl_pragmatic_catalog(
    provider,
    *,
    stop_event: threading.Event,
    progress: Progress,
    max_pages: int,
    on_game: GameCallback | None = None,
) -> list[Game]:
    """Enumerate the catalog from DOM + network responses + HTTP union fallback."""
    by_slug: dict[str, Game] = {}
    max_loads = max(1, int(max_pages))
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    diag_root = provider.provider_root / "catalog-diagnostics" / stamp
    diag_root.mkdir(parents=True, exist_ok=True)
    network_events: list[dict[str, Any]] = []
    network_serial = 0

    def ingest_games(games: list[Game], source: str) -> int:
        new_count = 0
        for game in games:
            current = by_slug.get(game.slug)
            if current is not None:
                fallback_name = _human_from_slug(game.slug)
                changed = False
                if current.name == fallback_name and game.name and game.name != fallback_name:
                    current.name = game.name
                    changed = True
                if not current.thumbnail_url and game.thumbnail_url:
                    current.thumbnail_url = game.thumbnail_url
                    changed = True
                if changed:
                    try:
                        provider._persist_catalog_artifacts(current, source)
                    except Exception:
                        pass
                continue

            by_slug[game.slug] = game
            new_count += 1
            try:
                provider._persist_catalog_artifacts(game, source)
            except Exception as exc:
                progress(f"[{game.name}] miniatura/metadata: {type(exc).__name__}: {exc}")
            if on_game is not None:
                on_game(game)
            progress(f"  + [{source}] {game.name} — {game.url}")
        return new_count

    def ingest_text(text: str, base_url: str, source: str) -> int:
        return ingest_games(extract_dynamic_games(provider, text, base_url), source)

    def write_network_event(event: dict[str, Any], batch: int) -> None:
        nonlocal network_serial
        network_serial += 1
        prefix = f"batch-{batch:04d}-response-{network_serial:05d}"
        body = event.get("body") or b""
        if isinstance(body, bytes):
            (diag_root / f"{prefix}.raw").write_bytes(body)
        meta = {key: value for key, value in event.items() if key not in {"body", "text"}}
        provider._write_json(diag_root / f"{prefix}.json", meta)

    progress(f"Catálogo Pragmatic robusto — {provider.catalog_url}")
    progress("Fuentes activas: DOM renderizado + XHR/fetch + JSON/HTML embebido + fallback HTTP.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
        page = context.new_page()

        def on_response(response: Response) -> None:
            try:
                request = response.request
                if request.resource_type not in {"xhr", "fetch"}:
                    return
                url = request.url
                if "pragmaticplay.com" not in urlparse(url).netloc.casefold():
                    return
                content_type = str(response.headers.get("content-type") or "")
                try:
                    body = response.body()
                except Exception:
                    body = b""
                if len(body) > 8 * 1024 * 1024:
                    body = body[: 8 * 1024 * 1024]
                network_events.append(
                    {
                        "url": url,
                        "status": response.status,
                        "resource_type": request.resource_type,
                        "content_type": content_type,
                        "body": body,
                        "text": body.decode("utf-8", errors="replace"),
                        "captured_at": utc_now_iso(),
                    }
                )
            except Exception:
                return

        context.on("response", on_response)
        try:
            page.goto(provider.catalog_url, wait_until="domcontentloaded", timeout=60_000)

            for label in ("Accept All", "Accept all", "Allow all", "I agree"):
                try:
                    button = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
                    if button.count() and button.first.is_visible():
                        button.first.click(timeout=1_500)
                        break
                except Exception:
                    pass

            page.wait_for_timeout(1_000)
            initial_new = ingest_text(page.content(), page.url, "DOM inicial")
            progress(f"DOM inicial: nuevos={initial_new}, total={len(by_slug)}")

            loads_done = 0
            no_growth = 0
            network_cursor = len(network_events)

            while loads_done < max_loads and not stop_event.is_set():
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.wait_for_timeout(350)

                control = _find_load_more(page)
                if control is None:
                    progress("Load More Games ya no está visible: fin de expansión dinámica.")
                    break

                before_total = len(by_slug)
                loads_done += 1
                try:
                    strategy = _click_load_more(page, control)
                except Exception as exc:
                    progress(f"Load More Games #{loads_done}: click falló: {type(exc).__name__}: {exc}")
                    break

                progress(f"Load More Games #{loads_done}: click={strategy}; esperando DOM/XHR...")
                deadline = time.monotonic() + 20.0
                processed_network = 0

                while time.monotonic() < deadline and not stop_event.is_set():
                    page.wait_for_timeout(300)

                    try:
                        ingest_text(page.content(), page.url, f"DOM load {loads_done}")
                    except Exception:
                        pass

                    while network_cursor < len(network_events):
                        event = network_events[network_cursor]
                        network_cursor += 1
                        processed_network += 1
                        extracted = ingest_text(
                            str(event.get("text") or ""),
                            str(event.get("url") or page.url),
                            f"XHR load {loads_done}",
                        )
                        event["extracted_games"] = extracted
                        write_network_event(event, loads_done)

                    if len(by_slug) > before_total:
                        page.wait_for_timeout(500)
                        while network_cursor < len(network_events):
                            event = network_events[network_cursor]
                            network_cursor += 1
                            processed_network += 1
                            extracted = ingest_text(
                                str(event.get("text") or ""),
                                str(event.get("url") or page.url),
                                f"XHR settle {loads_done}",
                            )
                            event["extracted_games"] = extracted
                            write_network_event(event, loads_done)
                        break

                    if _find_load_more(page) is None:
                        break

                growth = len(by_slug) - before_total
                progress(
                    f"Load More #{loads_done}: nuevos={growth}, total={len(by_slug)}, "
                    f"XHR/fetch procesados={processed_network}"
                )

                if growth > 0:
                    no_growth = 0
                    continue

                no_growth += 1
                if _find_load_more(page) is None:
                    break
                if no_growth >= 4:
                    progress("Cuatro cargas dinámicas sin juegos nuevos; se activa fallback HTTP.")
                    break
        finally:
            context.close()
            browser.close()

    # Numbered pages are only a union/fallback because CDN/cache state can make
    # arbitrary pages repeat. Do not stop after one duplicate page.
    if not stop_event.is_set():
        fallback_limit = min(200, max(60, max_loads if max_loads < 10_000 else 120))
        stagnant = 0
        progress(f"Fallback HTTP: verificando /page/N/ hasta N={fallback_limit} (unión por slug).")
        for page_no in range(2, fallback_limit + 1):
            if stop_event.is_set():
                break
            page_url = urljoin(provider.catalog_url, f"page/{page_no}/")
            try:
                response = provider.http.get(page_url, timeout=20.0, allow_redirects=True)
            except Exception as exc:
                progress(f"HTTP page/{page_no}: {type(exc).__name__}: {exc}")
                stagnant += 1
                if stagnant >= 12:
                    break
                continue
            if response.status_code == 404:
                progress(f"HTTP page/{page_no}: 404; fin del fallback.")
                break
            if response.status_code >= 400:
                stagnant += 1
                if stagnant >= 12:
                    break
                continue

            new_count = ingest_text(response.text, response.url, f"HTTP page/{page_no}")
            progress(f"HTTP page/{page_no}: nuevos={new_count}, total={len(by_slug)}")
            if new_count:
                stagnant = 0
            else:
                stagnant += 1
                if stagnant >= 12:
                    progress("Fallback HTTP: 12 páginas consecutivas sin slugs nuevos; fin.")
                    break

    games = sorted(by_slug.values(), key=lambda item: item.name.casefold())
    provider._write_catalog_index(games)
    provider._write_json(
        diag_root / "summary.json",
        {
            "schema": "tester-spin/pragmatic-catalog-diagnostics/v2",
            "source_url": provider.catalog_url,
            "finished_at": utc_now_iso(),
            "games": len(games),
            "network_responses_captured": network_serial,
            "diagnostics_dir": str(diag_root),
        },
    )
    progress(f"Catálogo Pragmatic terminado: {len(games)} juegos únicos.")
    progress(f"Diagnóstico del catálogo: {diag_root}")
    return games
