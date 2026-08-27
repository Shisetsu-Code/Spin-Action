from __future__ import annotations

import html
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from playwright.sync_api import sync_playwright

from tester_spin.models import Game, utc_now_iso
from tester_spin.providers.base import GameCallback, Progress
from tester_spin.providers.pragmatic import _human_from_slug


LOAD_MORE_RE = re.compile(r"(?:load\s+more\s+games|cargar\s+m[aá]s\s+juegos)", re.I)
GAME_URL_RE = re.compile(
    r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?games/([a-z0-9][a-z0-9_-]{1,100})/?(?:[?#].*)?$",
    re.I,
)
THUMB_NAME_RE = re.compile(
    r"^(?P<name>.+?)(?:[_-](?:\d{2,4}x\d{2,4}))(?:[_-](?:[A-Z]{2,5}))?$",
    re.I,
)
DATE_RE = re.compile(
    r"^(?:\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})$",
    re.I,
)
IGNORED_TEXT = {
    "play now",
    "play demo",
    "learn more",
    "read more",
    "load more games",
    "new",
}


LIVE_CARD_SNAPSHOT_JS = r"""
() => {
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4;
  };

  const attrMap = (el) => {
    const out = {};
    if (!el || !el.attributes) return out;
    for (const a of Array.from(el.attributes)) {
      if (a && a.name && a.value) out[a.name] = a.value;
    }
    return out;
  };

  const gameishImage = (img) => {
    const src = img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || '';
    const alt = img.alt || img.title || '';
    const lower = src.toLowerCase();
    if (!visible(img)) return false;
    if (/\b(?:339x180|338x180|340x180|300x160|600x320)\b/i.test(src)) return true;
    if (lower.includes('/wp-content/uploads/') && alt.trim().length >= 2) {
      const r = img.getBoundingClientRect();
      const ratio = r.width / Math.max(1, r.height);
      return ratio >= 1.35 && ratio <= 2.4;
    }
    return false;
  };

  const chooseCard = (img) => {
    let node = img;
    let best = img.parentElement || img;
    for (let i = 0; i < 8 && node && node.parentElement; i++) {
      node = node.parentElement;
      const imgs = Array.from(node.querySelectorAll('img')).filter(gameishImage);
      const controls = node.querySelectorAll('a,button,[role="button"],[data-href],[data-url],[data-game],[onclick]');
      const text = (node.innerText || node.textContent || '').trim();
      if (imgs.length >= 1 && imgs.length <= 3 && controls.length >= 1 && text.length <= 900) {
        best = node;
        if (imgs.length === 1) break;
      }
    }
    return best;
  };

  const cards = [];
  const seen = new Set();
  for (const img of Array.from(document.images).filter(gameishImage)) {
    const card = chooseCard(img);
    const key = (img.currentSrc || img.src || '') + '|' + (img.alt || '') + '|' + (card.innerText || '').slice(0, 120);
    if (seen.has(key)) continue;
    seen.add(key);

    const links = [];
    for (const el of Array.from(card.querySelectorAll('a,button,[role="button"],[data-href],[data-url],[data-game],[onclick]'))) {
      const attrs = attrMap(el);
      links.push({
        tag: el.tagName,
        text: (el.innerText || el.textContent || el.value || '').trim().slice(0, 300),
        href: el.href || el.getAttribute('href') || '',
        attrs,
      });
    }

    const cardAttrs = [];
    let p = card;
    for (let depth = 0; depth < 4 && p; depth++, p = p.parentElement) {
      cardAttrs.push({tag: p.tagName, attrs: attrMap(p)});
    }

    const r = img.getBoundingClientRect();
    cards.push({
      text: (card.innerText || card.textContent || '').trim().slice(0, 1200),
      image: {
        src: img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || '',
        raw_src: img.getAttribute('src') || '',
        alt: img.alt || '',
        title: img.title || '',
        width: r.width,
        height: r.height,
        attrs: attrMap(img),
      },
      links,
      card_attrs: cardAttrs,
    });
  }
  return cards;
}
"""


def _clean(value: Any) -> str:
    return html.unescape(str(value or "")).replace("\\/", "/").strip()


def _slug_from_url(value: str) -> str:
    text = _clean(value)
    if not text:
        return ""
    try:
        path = urlparse(text).path if "://" in text else urlparse(urljoin("https://www.pragmaticplay.com", text)).path
    except Exception:
        return ""
    match = GAME_URL_RE.search(path)
    return match.group(1).lower() if match else ""


def _slug_from_thumbnail(src: str) -> str:
    text = _clean(src)
    if not text:
        return ""
    filename = unquote(Path(urlparse(text).path).stem)
    match = THUMB_NAME_RE.match(filename)
    stem = match.group("name") if match else filename
    stem = re.sub(r"[_-](?:EN|ES|DE|FR|IT|PT|BR|PL|RO|RU|TR|JA|KO|TH|VI)$", "", stem, flags=re.I)
    stem = stem.replace("&", " and ")
    stem = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    return stem


def _candidate_text_lines(snapshot: dict[str, Any]) -> list[str]:
    values: list[str] = []
    image = snapshot.get("image") or {}
    for raw in (image.get("alt"), image.get("title")):
        value = re.sub(r"\s+", " ", _clean(raw)).strip(" -|–—")
        if value:
            values.append(value)

    for raw in str(snapshot.get("text") or "").splitlines():
        value = re.sub(r"\s+", " ", _clean(raw)).strip(" -|–—")
        if value:
            values.append(value)

    out: list[str] = []
    for value in values:
        folded = value.casefold()
        if folded in IGNORED_TEXT or DATE_RE.match(value):
            continue
        if len(value) < 2 or len(value) > 140:
            continue
        if value not in out:
            out.append(value)
    return out


def snapshot_to_game(snapshot: dict[str, Any], base_url: str) -> Game | None:
    """Turn one visible DOM card into a neutral Game record.

    Prefer a real /games/<slug>/ URL. When the frontend card has no permalink,
    recover the slug from the native thumbnail filename. The uploaded HAR that
    motivated this path contains Candy-Rush_339x180_EN.png after Load More.
    """
    image = snapshot.get("image") or {}
    values: list[str] = []

    for link in snapshot.get("links") or []:
        if not isinstance(link, dict):
            continue
        values.append(_clean(link.get("href")))
        for raw in (link.get("attrs") or {}).values():
            values.append(_clean(raw))

    for item in snapshot.get("card_attrs") or []:
        if not isinstance(item, dict):
            continue
        for raw in (item.get("attrs") or {}).values():
            values.append(_clean(raw))

    for raw in (image.get("src"), image.get("raw_src")):
        values.append(_clean(raw))
    for raw in (image.get("attrs") or {}).values():
        values.append(_clean(raw))

    slug = ""
    actual_url = ""
    for value in values:
        candidate = _slug_from_url(value)
        if candidate:
            slug = candidate
            actual_url = urljoin(base_url, value)
            break

    thumb = _clean(image.get("src") or image.get("raw_src"))
    if not slug:
        slug = _slug_from_thumbnail(thumb)
    if not slug:
        return None

    name_candidates = _candidate_text_lines(snapshot)
    fallback = _human_from_slug(slug)
    name = fallback
    for candidate in name_candidates:
        # Prefer text that resembles a human title instead of UI labels.
        if candidate.casefold() != fallback.casefold() and candidate.casefold() in IGNORED_TEXT:
            continue
        name = candidate
        break

    canonical = actual_url if actual_url and "/games/" in actual_url else f"https://www.pragmaticplay.com/en/games/{slug}/"
    return Game(
        provider="pragmatic",
        slug=slug,
        name=name,
        url=canonical,
        thumbnail_url=urljoin(base_url, thumb) if thumb else "",
    )


def _find_load_more(page):
    candidates = []
    for role in ("button", "link"):
        try:
            candidates.append(page.get_by_role(role, name=LOAD_MORE_RE))
        except Exception:
            pass
    try:
        candidates.append(page.get_by_text(LOAD_MORE_RE, exact=False))
    except Exception:
        pass

    for locator in candidates:
        try:
            for index in range(min(locator.count(), 20)):
                item = locator.nth(index)
                if item.is_visible():
                    return item
        except Exception:
            continue

    try:
        handle = page.evaluate_handle(
            """() => {
                const re = /(load\\s+more\\s+games|cargar\\s+m[aá]s\\s+juegos)/i;
                const nodes = Array.from(document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]'));
                return nodes.find(el => {
                    const s = getComputedStyle(el), r = el.getBoundingClientRect();
                    const label = (el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '').trim();
                    return re.test(label) && s.display !== 'none' && s.visibility !== 'hidden' && r.width > 4 && r.height > 4;
                }) || null;
            }"""
        )
        return handle.as_element()
    except Exception:
        return None


def _click_load_more(page, locator) -> str:
    try:
        locator.scroll_into_view_if_needed(timeout=4_000)
    except Exception:
        pass

    try:
        locator.hover(timeout=2_000)
    except Exception:
        pass

    try:
        locator.click(timeout=5_000)
        return "playwright"
    except Exception:
        pass

    try:
        box = locator.bounding_box()
        if box:
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.down()
            page.wait_for_timeout(60)
            page.mouse.up()
            return "mouse"
    except Exception:
        pass

    try:
        locator.click(timeout=5_000, force=True)
        return "force"
    except Exception:
        pass

    locator.evaluate(
        """el => {
            const target = el.closest('button,a,[role="button"]') || el;
            target.scrollIntoView({block:'center'});
            target.click();
        }"""
    )
    return "dom-click"


def _snapshot(page) -> list[dict[str, Any]]:
    value = page.evaluate(LIVE_CARD_SNAPSHOT_JS)
    return value if isinstance(value, list) else []


def _fingerprints(snapshots: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for item in snapshots:
        game = snapshot_to_game(item, "https://www.pragmaticplay.com/en/games/")
        image = item.get("image") or {}
        key = game.slug if game is not None else _clean(image.get("src"))
        if key:
            out.add(key)
    return out


def crawl_pragmatic_catalog_dom(
    provider,
    *,
    stop_event: threading.Event,
    progress: Progress,
    max_pages: int,
    on_game: GameCallback | None = None,
) -> list[Game]:
    by_slug: dict[str, Game] = {}
    max_loads = max(1, int(max_pages))
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    diag_root = provider.provider_root / "catalog-diagnostics" / f"{stamp}-visible-dom"
    diag_root.mkdir(parents=True, exist_ok=True)

    def ingest(snapshots: list[dict[str, Any]], source: str) -> int:
        new_count = 0
        for snap in snapshots:
            game = snapshot_to_game(snap, provider.catalog_url)
            if game is None:
                continue
            current = by_slug.get(game.slug)
            if current is not None:
                if not current.thumbnail_url and game.thumbnail_url:
                    current.thumbnail_url = game.thumbnail_url
                if current.name == _human_from_slug(game.slug) and game.name != current.name:
                    current.name = game.name
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

    progress(f"Catálogo Pragmatic visible-DOM — {provider.catalog_url}")
    progress("HAR observado: Load More no usa XHR; se espera crecimiento de tarjetas/imágenes visibles.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, locale="en-US")
        page = context.new_page()
        try:
            page.goto(provider.catalog_url, wait_until="domcontentloaded", timeout=60_000)
            for label in ("Accept All", "Accept all", "Allow all", "I agree"):
                try:
                    control = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
                    if control.count() and control.first.is_visible():
                        control.first.click(timeout=1_500)
                        break
                except Exception:
                    pass

            page.wait_for_timeout(1_000)
            initial = _snapshot(page)
            provider._write_json(diag_root / "visible-cards-0000.json", initial)
            ingest(initial, "visible-dom:0")
            progress(f"DOM visible inicial: tarjetas={len(initial)}, juegos={len(by_slug)}")

            loads_done = 0
            no_growth = 0
            while loads_done < max_loads and not stop_event.is_set():
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.wait_for_timeout(350)

                control = _find_load_more(page)
                if control is None:
                    progress("Load More Games ya no está visible: catálogo agotado.")
                    break

                before = _snapshot(page)
                before_keys = _fingerprints(before)
                loads_done += 1
                progress(
                    f"Load More #{loads_done}/{max_loads}: visibles={len(before)}, "
                    f"firmas={len(before_keys)}, guardados={len(by_slug)}"
                )

                try:
                    strategy = _click_load_more(page, control)
                except Exception as exc:
                    progress(f"No se pudo activar Load More: {type(exc).__name__}: {exc}")
                    break

                deadline = time.monotonic() + 15.0
                after = before
                after_keys = before_keys
                while time.monotonic() < deadline and not stop_event.is_set():
                    page.wait_for_timeout(250)
                    after = _snapshot(page)
                    after_keys = _fingerprints(after)
                    if len(after_keys - before_keys) > 0 or len(after) > len(before):
                        break

                provider._write_json(diag_root / f"visible-cards-{loads_done:04d}.json", after)
                try:
                    (diag_root / f"dom-{loads_done:04d}.html").write_text(page.content(), encoding="utf-8")
                except Exception:
                    pass

                new_visible = sorted(after_keys - before_keys)
                new_count = ingest(after, f"visible-dom:{loads_done}")
                progress(
                    f"Load More #{loads_done}: click={strategy}, tarjetas={len(after)}, "
                    f"nuevas-visibles={len(new_visible)}, nuevos-guardados={new_count}, total={len(by_slug)}"
                )

                if new_visible or new_count:
                    no_growth = 0
                    continue

                no_growth += 1
                progress(f"Click sin crecimiento visible ({no_growth}/2). Diagnóstico DOM guardado.")
                if no_growth >= 2:
                    break

        finally:
            context.close()
            browser.close()

    games = sorted(by_slug.values(), key=lambda item: item.name.casefold())
    provider._write_catalog_index(games)
    provider._write_json(
        diag_root / "summary.json",
        {
            "schema": "tester-spin/pragmatic-visible-dom-catalog/v1",
            "updated_at": utc_now_iso(),
            "count": len(games),
            "loads_done": loads_done,
            "har_observation": "Load More click produced lazy-loaded game image request but no catalog XHR/fetch.",
            "games": [{"name": g.name, "slug": g.slug, "url": g.url, "thumbnail_url": g.thumbnail_url} for g in games],
        },
    )
    progress(f"Catálogo visible-DOM terminado: {len(games)} juegos; Load More ejecutado {loads_done} vez/veces.")
    return games
