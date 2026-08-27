from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from tester_spin.models import Game, utc_now_iso
from tester_spin.providers.base import GameCallback, Progress
from tester_spin.providers.pragmatic import _human_from_slug
from tester_spin.providers.pragmatic_catalog_dom import (
    _click_load_more,
    _find_load_more,
    _snapshot,
    snapshot_to_game,
)


ALL_GAME_CARDS_JS = r"""
() => {
  const attrs = (el) => {
    const out = {};
    if (!el || !el.attributes) return out;
    for (const a of Array.from(el.attributes)) {
      if (a.name && a.value) out[a.name] = a.value;
    }
    return out;
  };

  const sources = (img) => [
    img.getAttribute('data-src') || '',
    img.getAttribute('data-lazy-src') || '',
    img.getAttribute('data-original') || '',
    img.getAttribute('src') || '',
    img.currentSrc || '',
    img.getAttribute('data-srcset') || '',
    img.getAttribute('srcset') || '',
  ].filter(Boolean);

  const hasGameControl = (img) => {
    let node = img;
    for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
      if (!node.querySelector) continue;
      if (node.querySelector(
        'a[href*="/games/"],[data-href*="/games/"],[data-url*="/games/"],[data-game],[data-slug]'
      )) return true;
    }
    return false;
  };

  const isGameImage = (img) => {
    const joined = sources(img).join(' ');
    if (/(?:339x180|338x180|340x180|300x160|600x320)/i.test(joined)) return true;
    return hasGameControl(img);
  };

  const bestSource = (img) => {
    const all = sources(img);
    return all.find(v => /(?:339x180|338x180|340x180|300x160|600x320)/i.test(v)) ||
           all.find(v => /\/wp-content\/uploads\//i.test(v)) ||
           all[0] || '';
  };

  const cardFor = (img) => {
    let node = img;
    let best = img.parentElement || img;
    for (let i = 0; i < 9 && node && node.parentElement; i++) {
      node = node.parentElement;
      const gameLinks = node.querySelectorAll(
        'a[href*="/games/"],[data-href*="/games/"],[data-url*="/games/"],[data-game],[data-slug]'
      );
      const gameImages = Array.from(node.querySelectorAll('img')).filter(isGameImage);
      const text = (node.textContent || '').trim();
      if (gameImages.length >= 1 && gameImages.length <= 4 && (gameLinks.length || text)) {
        best = node;
        if (gameImages.length === 1) break;
      }
    }
    return best;
  };

  const out = [];
  const seen = new Set();
  for (const img of Array.from(document.querySelectorAll('img')).filter(isGameImage)) {
    const card = cardFor(img);
    const src = bestSource(img);
    const key = src + '|' + (img.alt || '') + '|' + (card.textContent || '').slice(0, 160);
    if (seen.has(key)) continue;
    seen.add(key);

    const links = [];
    for (const el of Array.from(card.querySelectorAll(
      'a,button,[role="button"],[data-href],[data-url],[data-game],[data-slug],[onclick]'
    ))) {
      links.push({
        tag: el.tagName,
        text: (el.textContent || el.value || '').trim().slice(0, 300),
        href: el.href || el.getAttribute('href') || '',
        attrs: attrs(el),
      });
    }

    const cardAttrs = [];
    let p = card;
    for (let depth = 0; depth < 5 && p; depth++, p = p.parentElement) {
      cardAttrs.push({tag: p.tagName, attrs: attrs(p)});
    }

    out.push({
      text: (card.textContent || '').trim().slice(0, 1200),
      image: {
        src,
        raw_src: img.getAttribute('src') || '',
        alt: img.alt || '',
        title: img.title || '',
        attrs: attrs(img),
      },
      links,
      card_attrs: cardAttrs,
    });
  }
  return out;
}
"""


MUTATION_JS = r"""
() => {
  if (window.__testerSpinMutationState) return;
  const state = {seq: 0, last: performance.now()};
  new MutationObserver((records) => {
    state.seq += Math.max(1, records.length);
    state.last = performance.now();
  }).observe(document.documentElement, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['class','style','hidden','aria-hidden','src','srcset','data-src','data-lazy-src']
  });
  window.__testerSpinMutationState = state;
}
"""


def _all_cards(page) -> list[dict[str, Any]]:
    try:
        value = page.evaluate(ALL_GAME_CARDS_JS)
        return value if isinstance(value, list) else []
    except Exception:
        return []


def _key(snapshot: dict[str, Any], base_url: str) -> str:
    game = snapshot_to_game(snapshot, base_url)
    if game is not None:
        return game.slug
    image = snapshot.get("image") or {}
    return str(image.get("src") or image.get("raw_src") or "")


def _keys(items: list[dict[str, Any]], base_url: str) -> set[str]:
    return {value for item in items if (value := _key(item, base_url))}


def _delta(items: list[dict[str, Any]], seen: set[str], base_url: str) -> list[dict[str, Any]]:
    return [item for item in items if (value := _key(item, base_url)) and value not in seen]


def _mutation_seq(page) -> int:
    try:
        return int(page.evaluate("() => Number((window.__testerSpinMutationState || {}).seq || 0)") or 0)
    except Exception:
        return 0


def _wait_mutation(page, before: int) -> None:
    try:
        page.wait_for_function(
            "n => Number((window.__testerSpinMutationState || {}).seq || 0) > n",
            arg=before,
            timeout=800,
        )
    except Exception:
        page.wait_for_timeout(80)
    try:
        page.wait_for_function(
            "() => performance.now() - Number((window.__testerSpinMutationState || {}).last || 0) > 80",
            timeout=450,
        )
    except Exception:
        pass


_PERSIST_LOCAL = threading.local()


def _persist(provider, game: Game, source: str) -> tuple[Game, str]:
    try:
        worker_provider = getattr(_PERSIST_LOCAL, "provider", None)
        if worker_provider is None:
            worker_provider = provider.__class__(provider.data_root, base_bet=provider.base_bet)
            worker_provider.catalog_url = provider.catalog_url
            _PERSIST_LOCAL.provider = worker_provider
        worker_provider._persist_catalog_artifacts(game, source)
        return game, ""
    except Exception as exc:
        return game, f"{type(exc).__name__}: {exc}"


def crawl_pragmatic_catalog_preloaded(
    provider,
    *,
    stop_event: threading.Event,
    progress: Progress,
    max_pages: int,
    on_game: GameCallback | None = None,
) -> list[Game]:
    """Enumerate hidden/preloaded cards first; click only when structure actually grows."""
    from playwright.sync_api import sync_playwright

    by_slug: dict[str, Game] = {}
    source_by_slug: dict[str, str] = {}
    max_loads = max(1, int(max_pages))
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    diag = provider.provider_root / "catalog-diagnostics" / f"{stamp}-preloaded-dom"
    diag.mkdir(parents=True, exist_ok=True)

    def ingest(items: list[dict[str, Any]], source: str) -> int:
        added = 0
        for item in items:
            game = snapshot_to_game(item, provider.catalog_url)
            if game is None:
                continue
            old = by_slug.get(game.slug)
            if old is not None:
                if not old.thumbnail_url and game.thumbnail_url:
                    old.thumbnail_url = game.thumbnail_url
                if old.name == _human_from_slug(game.slug) and game.name != old.name:
                    old.name = game.name
                continue
            by_slug[game.slug] = game
            source_by_slug[game.slug] = source
            added += 1
            if on_game is not None:
                on_game(game)
        return added

    loads_done = 0
    preloaded_complete = False
    last_all: list[dict[str, Any]] = []
    no_growth_streak = 0

    progress("Catálogo ULTRA: leyendo tarjetas ocultas/preloaded antes de pulsar Load More.")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, locale="en-US")
        page = context.new_page()
        try:
            page.goto(provider.catalog_url, wait_until="domcontentloaded", timeout=60_000)
            for label in ("Accept All", "Accept all", "Allow all", "I agree"):
                try:
                    btn = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
                    if btn.count() and btn.first.is_visible():
                        btn.first.click(timeout=1_000)
                        break
                except Exception:
                    pass

            page.evaluate(MUTATION_JS)
            page.wait_for_timeout(120)

            visible = _snapshot(page)
            all_cards = _all_cards(page) or visible
            all_keys = _keys(all_cards, provider.catalog_url)
            hidden_preloaded = max(0, len(all_cards) - len(visible))
            ingest(all_cards, "preloaded-dom:0")
            provider._write_json(diag / "all-cards-0000.json", all_cards)
            progress(
                f"Inicial: visibles={len(visible)}, DOM total={len(all_cards)}, "
                f"ocultas/preloaded={hidden_preloaded}, juegos={len(by_slug)}"
            )

            while loads_done < max_loads and not stop_event.is_set():
                button = _find_load_more(page)
                if button is None:
                    progress("Load More Games desapareció: catálogo agotado.")
                    break

                before_keys = all_keys
                before_seq = _mutation_seq(page)
                loads_done += 1
                strategy = _click_load_more(page, button)
                _wait_mutation(page, before_seq)

                after = _all_cards(page) or _snapshot(page)
                after_keys = _keys(after, provider.catalog_url)
                added_cards = _delta(after, before_keys, provider.catalog_url)

                if not added_cards:
                    page.wait_for_timeout(150)
                    after = _all_cards(page) or _snapshot(page)
                    after_keys = _keys(after, provider.catalog_url)
                    added_cards = _delta(after, before_keys, provider.catalog_url)

                added_games = ingest(added_cards, f"load-more:{loads_done}")
                all_cards = after
                all_keys = after_keys
                progress(
                    f"Load More #{loads_done}: {strategy}; estructuras nuevas={len(added_cards)}, "
                    f"juegos nuevos={added_games}, total={len(by_slug)}"
                )

                if added_cards:
                    no_growth_streak = 0
                    provider._write_json(diag / f"delta-{loads_done:04d}.json", added_cards)
                    continue

                no_growth_streak += 1

                # Strong case: we already saw hidden game cards before clicking. If a
                # click changes no structure, it is only revealing/lazy-loading cards
                # we already captured, so further clicks add no catalog information.
                if hidden_preloaded > 0:
                    preloaded_complete = True
                    progress(
                        "Sin estructuras nuevas y ya había tarjetas ocultas precargadas: "
                        "catálogo obtenido sin recorrer todas las tandas visuales."
                    )
                    break

                # Inconclusive DOM: do NOT repeat the old premature-stop bug. Keep
                # clicking while the button survives, allowing intermittent no-op or
                # animation-only batches. Eight consecutive no-growth clicks is the
                # safety guard, not one or two.
                if _find_load_more(page) is None:
                    progress("Sin crecimiento y el botón desapareció: catálogo agotado.")
                    break
                if no_growth_streak >= 8:
                    progress("Ocho clicks consecutivos sin estructuras nuevas; se detiene por guardia de seguridad.")
                    provider._write_json(diag / f"stall-{loads_done:04d}.json", after)
                    try:
                        (diag / f"dom-stall-{loads_done:04d}.html").write_text(
                            page.content(), encoding="utf-8"
                        )
                    except Exception:
                        pass
                    break

                progress(
                    f"Click sin crecimiento concluyente ({no_growth_streak}/8); "
                    "el botón sigue activo, se continúa."
                )

            last_all = all_cards
        except Exception:
            try:
                provider._write_json(diag / "all-cards-error.json", _all_cards(page))
                (diag / "dom-error.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()

    games = sorted(by_slug.values(), key=lambda game: game.name.casefold())
    provider._write_catalog_index(games)
    provider._write_json(diag / "all-cards-final.json", last_all)

    if games and not stop_event.is_set():
        workers = min(8, max(4, len(games) // 30 + 1))
        progress(f"Enumeración lista: {len(games)} juegos. Descargando miniaturas aparte ({workers} workers).")
        done = 0
        errors = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="catalog-artifact") as pool:
            futures = [
                pool.submit(_persist, provider, game, source_by_slug.get(game.slug, provider.catalog_url))
                for game in games
            ]
            for future in as_completed(futures):
                game, error = future.result()
                done += 1
                if error:
                    errors += 1
                    progress(f"[{game.name}] miniatura/metadata: {error}")
                if on_game is not None:
                    on_game(game)
                if done % 25 == 0 or done == len(games):
                    progress(f"Miniaturas: {done}/{len(games)}; errores={errors}")

    provider._write_catalog_index(games)
    provider._write_json(
        diag / "summary.json",
        {
            "schema": "tester-spin/pragmatic-preloaded-catalog/v2",
            "updated_at": utc_now_iso(),
            "count": len(games),
            "loads_done": loads_done,
            "hidden_preloaded_initial": hidden_preloaded if 'hidden_preloaded' in locals() else 0,
            "preloaded_complete": preloaded_complete,
            "strategy": "hidden/preloaded DOM first; structural Load More fallback; 8-click inconclusive guard",
            "games": [
                {
                    "name": g.name,
                    "slug": g.slug,
                    "url": g.url,
                    "thumbnail_url": g.thumbnail_url,
                    "thumbnail_path": g.thumbnail_path,
                }
                for g in games
            ],
        },
    )
    progress(f"Catálogo ULTRA terminado: {len(games)} juegos; clicks={loads_done}.")
    return games
