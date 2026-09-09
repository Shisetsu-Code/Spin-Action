from __future__ import annotations

import queue
import threading
from tkinter import messagebox, ttk

from tester_spin.app import TesterSpinApp
from tester_spin.models import Game


def _catalog_shrink_suspicious(previous_count: int, current_count: int) -> bool:
    previous = max(0, int(previous_count))
    current = max(0, int(current_count))
    return previous >= 100 and current < max(25, int(previous * 0.60))


class LiveTesterSpinApp(TesterSpinApp):
    """GUI variant that streams catalog discoveries into the table immediately."""

    def __init__(self) -> None:
        super().__init__()
        # Zero means "continue until the site's Load More control disappears".
        self.max_pages_var.set("0")
        self._rename_catalog_limit_label(self)

    def _rename_catalog_limit_label(self, widget) -> None:
        for child in widget.winfo_children():
            try:
                if isinstance(child, ttk.Label) and child.cget("text") in {
                    "Máx. páginas:",
                    "Máx. cargas:",
                    "Máx. páginas HTTP:",
                    "Máx. cargas dinámicas:",
                }:
                    child.configure(text="Máx. cargas (0=todas):")
            except Exception:
                pass
            self._rename_catalog_limit_label(child)

    def _start_crawl(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        try:
            requested_loads = int(self.max_pages_var.get())
            if requested_loads < 0:
                raise ValueError
            full_catalog_requested = requested_loads == 0
            # The provider still gets a finite loop guard. In normal use it exits
            # much earlier when "Load More Games" disappears.
            max_pages = 10_000 if full_catalog_requested else max(1, requested_loads)
        except ValueError:
            messagebox.showerror("Tester-Spin", "Máx. cargas debe ser 0 o un entero positivo.")
            return

        provider = self._provider()
        provider.catalog_url = self.catalog_url_var.get().strip() or provider.catalog_url
        self._stop_event = threading.Event()
        self._set_busy(True)
        self.status_var.set("Cargando catálogo completo...")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)

        def on_game(game: Game) -> None:
            # Stream directly to Tk. Persistence is committed in one provider batch
            # after the crawl, avoiding one remote D1 request per catalogue item.
            self._events.put(("catalog_game", game))

        def worker() -> None:
            try:
                previous_games = self.storage.list_games(provider.key)
                previous_count = len(previous_games)

                provider.set_catalog_authority(True, "")
                games = provider.crawl_catalog(
                    stop_event=self._stop_event,
                    progress=lambda message: self._events.put(("log", message)),
                    max_pages=max_pages,
                    on_game=on_game,
                )
                self.storage.upsert_games(games)

                authoritative = bool(
                    getattr(provider, "catalog_crawl_authoritative", True)
                )
                authority_reason = str(
                    getattr(provider, "catalog_crawl_reason", "") or ""
                )

                # Even an authoritative crawler must fail closed on catastrophic
                # shrinkage. A provider catalog may legitimately change, but losing
                # most rows in one crawl is far more likely to be a WAF/DOM/parser
                # regression than hundreds of simultaneous removals.
                shrink_suspicious = _catalog_shrink_suspicious(
                    previous_count,
                    len(games),
                )

                can_reconcile = (
                    full_catalog_requested
                    and not self._stop_event.is_set()
                    and bool(games)
                    and authoritative
                    and not shrink_suspicious
                )

                if can_reconcile:
                    removed = self.storage.reconcile_provider_games(
                        provider.key,
                        {game.slug for game in games},
                    )
                    self._events.put(
                        (
                            "log",
                            f"Reconciliación de catálogo: actuales={len(games)}, obsoletos eliminados={removed}.",
                        )
                    )
                elif full_catalog_requested and not self._stop_event.is_set():
                    reasons: list[str] = []
                    if not authoritative:
                        reasons.append(
                            authority_reason or "crawler/fuente no autoritativa"
                        )
                    if shrink_suspicious:
                        reasons.append(
                            f"reducción anómala {previous_count}→{len(games)}"
                        )
                    if not games:
                        reasons.append("crawl vacío")
                    self._events.put(
                        (
                            "log",
                            "RECONCILIACIÓN BLOQUEADA: no se eliminará ningún juego"
                            + (f" ({'; '.join(reasons)})." if reasons else "."),
                        )
                    )

                self._events.put(("catalog_done", len(games)))
            except Exception as exc:
                self._events.put(("error", f"Catálogo: {type(exc).__name__}: {exc}"))

        self._worker = threading.Thread(target=worker, daemon=True, name="catalog-crawler")
        self._worker.start()

    def _upsert_catalog_game(self, game: Game) -> None:
        iid = f"{game.provider}::{game.slug}"
        current = self._games.get(iid)
        if current is not None and not game.last_test_at:
            # Fresh catalogue objects do not carry historical test state. Preserve
            # the row state already loaded from SQLite/D1 while streaming a recrawl.
            game.last_status = current.last_status
            game.last_error = current.last_error
            game.last_test_at = current.last_test_at
            game.last_latency_ms = current.last_latency_ms
            if not game.symbol:
                game.symbol = current.symbol
        self._games[iid] = game
        image = self._load_tree_thumbnail(game.thumbnail_path)
        values = (
            game.name,
            game.symbol or "—",
            game.last_status or "PENDIENTE",
            game.last_test_at or "—",
            game.url,
        )
        if self.tree.exists(iid):
            self.tree.item(iid, text="", image=image, values=values)
        else:
            self.tree.insert("", "end", iid=iid, text="", image=image, values=values)
        self.count_var.set(f"{len(self._games)} juegos")

    def _drain_events(self) -> None:
        deferred: list[tuple[str, object]] = []
        while True:
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "catalog_game":
                if isinstance(value, Game):
                    self._upsert_catalog_game(value)
                else:
                    provider_key, slug = value  # type: ignore[misc]
                    game = self.storage.get_game(str(provider_key), str(slug))
                    if game is not None:
                        self._upsert_catalog_game(game)
            else:
                deferred.append((kind, value))

        for event in deferred:
            self._events.put(event)
        super()._drain_events()


def main() -> None:
    LiveTesterSpinApp().mainloop()


if __name__ == "__main__":
    main()
