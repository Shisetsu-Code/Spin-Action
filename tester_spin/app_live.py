from __future__ import annotations

import queue
import threading
from tkinter import messagebox, ttk

from tester_spin.app import TesterSpinApp
from tester_spin.models import Game


class LiveTesterSpinApp(TesterSpinApp):
    """GUI variant that streams catalog discoveries into the table immediately."""

    def __init__(self) -> None:
        super().__init__()
        self._rename_catalog_limit_label(self)

    def _rename_catalog_limit_label(self, widget) -> None:
        for child in widget.winfo_children():
            try:
                if isinstance(child, ttk.Label) and child.cget("text") in {"Máx. páginas:", "Máx. cargas:"}:
                    child.configure(text="Máx. páginas HTTP:")
            except Exception:
                pass
            self._rename_catalog_limit_label(child)

    def _start_crawl(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        try:
            max_pages = max(1, int(self.max_pages_var.get()))
        except ValueError:
            messagebox.showerror("Tester-Spin", "Máx. páginas HTTP debe ser un entero.")
            return

        provider = self._provider()
        provider.catalog_url = self.catalog_url_var.get().strip() or provider.catalog_url
        self._stop_event = threading.Event()
        self._set_busy(True)
        self.status_var.set("Cargando catálogo por HTTP...")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)

        def on_game(game: Game) -> None:
            self.storage.upsert_games([game])
            self._events.put(("catalog_game", (game.provider, game.slug)))

        def worker() -> None:
            try:
                games = provider.crawl_catalog(
                    stop_event=self._stop_event,
                    progress=lambda message: self._events.put(("log", message)),
                    max_pages=max_pages,
                    on_game=on_game,
                )
                self.storage.upsert_games(games)
                self._events.put(("catalog_done", len(games)))
            except Exception as exc:
                self._events.put(("error", f"Catálogo: {type(exc).__name__}: {exc}"))

        self._worker = threading.Thread(target=worker, daemon=True, name="catalog-crawler")
        self._worker.start()

    def _upsert_catalog_row(self, provider_key: str, slug: str) -> None:
        game = self.storage.get_game(provider_key, slug)
        if game is None:
            return
        iid = f"{game.provider}::{game.slug}"
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
                provider_key, slug = value  # type: ignore[misc]
                self._upsert_catalog_row(str(provider_key), str(slug))
            else:
                deferred.append((kind, value))

        for event in deferred:
            self._events.put(event)
        super()._drain_events()


def main() -> None:
    LiveTesterSpinApp().mainloop()


if __name__ == "__main__":
    main()
