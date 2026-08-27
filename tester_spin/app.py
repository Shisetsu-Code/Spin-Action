from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from tester_spin.models import Game, GameTestResult
from tester_spin.providers import PragmaticProvider, ProviderRegistry
from tester_spin.scheduler import run_game_tests
from tester_spin.storage import Storage


class TesterSpinApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Tester-Spin")
        self.geometry("1500x940")
        self.minsize(1180, 760)

        self.root_dir = Path.cwd()
        self.data_root = self.root_dir / "data"
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.storage = Storage(self.data_root / "tester-spin.sqlite3")

        self.registry = ProviderRegistry()
        self.registry.register(PragmaticProvider(self.data_root))
        self._display_to_key = {provider.display_name: provider.key for provider in self.registry.all()}

        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._games: dict[str, Game] = {}
        self._thumb_images: dict[str, ImageTk.PhotoImage] = {}
        self._preview_image: ImageTk.PhotoImage | None = None
        self._test_total = 0
        self._test_done = 0

        providers = list(self._display_to_key)
        self.provider_var = tk.StringVar(value=providers[0] if providers else "")
        self.catalog_url_var = tk.StringVar()
        self.max_pages_var = tk.StringVar(value="100")
        self.concurrency_var = tk.StringVar(value="3")
        self.spins_var = tk.StringVar(value="1")
        self.delay_var = tk.StringVar(value="1.0")
        self.timeout_var = tk.StringVar(value="30")
        self.status_var = tk.StringVar(value="Listo")
        self.count_var = tk.StringVar(value="0 juegos")
        self.selection_var = tk.StringVar(value="Sin selección")

        self._build()
        self._on_provider_changed()
        self._refresh_games()
        self.after(100, self._drain_events)

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        top = ttk.LabelFrame(outer, text="Proveedor y catálogo", padding=10)
        top.pack(fill="x")
        row = ttk.Frame(top)
        row.pack(fill="x")

        ttk.Label(row, text="Proveedor:").pack(side="left")
        self.provider_combo = ttk.Combobox(
            row,
            textvariable=self.provider_var,
            values=list(self._display_to_key),
            state="readonly",
            width=22,
        )
        self.provider_combo.pack(side="left", padx=(6, 14))
        self.provider_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_provider_changed())

        ttk.Label(row, text="URL catálogo:").pack(side="left")
        ttk.Entry(row, textvariable=self.catalog_url_var).pack(side="left", fill="x", expand=True, padx=(6, 10))
        ttk.Label(row, text="Máx. páginas:").pack(side="left")
        ttk.Entry(row, textvariable=self.max_pages_var, width=7).pack(side="left", padx=(6, 10))
        self.crawl_btn = ttk.Button(row, text="CARGAR / ACTUALIZAR CATÁLOGO", command=self._start_crawl)
        self.crawl_btn.pack(side="left")

        opts = ttk.LabelFrame(outer, text="Prueba de juegos", padding=10)
        opts.pack(fill="x", pady=(10, 0))
        row2 = ttk.Frame(opts)
        row2.pack(fill="x")

        for label, var, width in (
            ("Juegos simultáneos", self.concurrency_var, 7),
            ("Repeticiones por modo", self.spins_var, 7),
            ("Delay entre juegos (s)", self.delay_var, 7),
            ("Timeout (s)", self.timeout_var, 7),
        ):
            ttk.Label(row2, text=label + ":").pack(side="left", padx=(0 if label == "Juegos simultáneos" else 16, 5))
            ttk.Entry(row2, textvariable=var, width=width).pack(side="left")

        self.test_selected_btn = ttk.Button(row2, text="PROBAR SELECCIONADOS", command=self._test_selected)
        self.test_selected_btn.pack(side="left", padx=(20, 8))
        self.test_all_btn = ttk.Button(row2, text="PROBAR TODOS", command=self._test_all)
        self.test_all_btn.pack(side="left")
        self.stop_btn = ttk.Button(row2, text="DETENER", command=self._stop, state="disabled")
        self.stop_btn.pack(side="right")

        ttk.Label(
            opts,
            text=(
                "Cada juego descubre automáticamente SPIN base, todos los ante-bet/variantes declarados por bls, "
                "todas las compras declaradas por purInit/purInit_e y conserva evidencia RAW de estados/modos aún no automatizados."
            ),
            wraplength=1400,
        ).pack(anchor="w", pady=(8, 0))

        status_row = ttk.Frame(outer)
        status_row.pack(fill="x", pady=(8, 0))
        ttk.Label(status_row, textvariable=self.status_var).pack(side="left")
        ttk.Label(status_row, textvariable=self.count_var).pack(side="left", padx=(18, 0))
        self.progress = ttk.Progressbar(status_row, mode="determinate", maximum=100, value=0)
        self.progress.pack(side="right", fill="x", expand=True, padx=(20, 0))

        middle = ttk.Panedwindow(outer, orient="horizontal")
        middle.pack(fill="both", expand=True, pady=(8, 0))

        list_frame = ttk.LabelFrame(middle, text="Juegos", padding=6)
        detail_frame = ttk.LabelFrame(middle, text="Detalle", padding=10)
        middle.add(list_frame, weight=4)
        middle.add(detail_frame, weight=2)

        columns = ("name", "symbol", "status", "tested", "url")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Miniatura")
        self.tree.heading("name", text="Nombre")
        self.tree.heading("symbol", text="ID proveedor")
        self.tree.heading("status", text="Estado")
        self.tree.heading("tested", text="Última prueba")
        self.tree.heading("url", text="Link")
        self.tree.column("#0", width=110, minwidth=100, stretch=False)
        self.tree.column("name", width=260, minwidth=180)
        self.tree.column("symbol", width=130, minwidth=90)
        self.tree.column("status", width=90, minwidth=80, anchor="center")
        self.tree.column("tested", width=155, minwidth=120)
        self.tree.column("url", width=440, minwidth=250)
        yscroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(list_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._show_selected_game())
        self.tree.bind("<Double-1>", lambda _event: self._open_selected_url())

        self.preview_label = ttk.Label(detail_frame, text="Sin miniatura", anchor="center")
        self.preview_label.pack(fill="x", pady=(0, 10))
        ttk.Label(detail_frame, textvariable=self.selection_var, font=("TkDefaultFont", 12, "bold"), wraplength=420).pack(anchor="w")
        self.detail_text = tk.Text(detail_frame, height=18, wrap="word", state="disabled")
        self.detail_text.pack(fill="both", expand=True, pady=(8, 8))
        detail_actions = ttk.Frame(detail_frame)
        detail_actions.pack(fill="x")
        ttk.Button(detail_actions, text="Abrir juego", command=self._open_selected_url).pack(side="left")
        ttk.Button(detail_actions, text="Abrir carpeta", command=self._open_selected_folder).pack(side="left", padx=(8, 0))
        ttk.Button(detail_actions, text="Probar este juego", command=self._test_selected).pack(side="left", padx=(8, 0))

        log_frame = ttk.LabelFrame(outer, text="Log", padding=6)
        log_frame.pack(fill="both", expand=False, pady=(8, 0))
        self.log = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)

    def _provider(self):
        key = self._display_to_key[self.provider_var.get()]
        return self.registry.get(key)

    def _on_provider_changed(self) -> None:
        provider = self._provider()
        self.catalog_url_var.set(provider.catalog_url)
        self._refresh_games()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.crawl_btn.configure(state=state)
        self.test_selected_btn.configure(state=state)
        self.test_all_btn.configure(state=state)
        self.provider_combo.configure(state="disabled" if busy else "readonly")
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def _start_crawl(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        try:
            max_pages = max(1, int(self.max_pages_var.get()))
        except ValueError:
            messagebox.showerror("Tester-Spin", "Máx. páginas debe ser un entero.")
            return
        provider = self._provider()
        provider.catalog_url = self.catalog_url_var.get().strip() or provider.catalog_url
        self._stop_event = threading.Event()
        self._set_busy(True)
        self.status_var.set("Cargando catálogo...")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)

        def worker() -> None:
            try:
                games = provider.crawl_catalog(
                    stop_event=self._stop_event,
                    progress=lambda message: self._events.put(("log", message)),
                    max_pages=max_pages,
                )
                self.storage.upsert_games(games)
                self._events.put(("catalog_done", len(games)))
            except Exception as exc:
                self._events.put(("error", f"Catálogo: {type(exc).__name__}: {exc}"))

        self._worker = threading.Thread(target=worker, daemon=True, name="catalog-crawler")
        self._worker.start()

    def _test_selected(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Tester-Spin", "Seleccioná al menos un juego.")
            return
        games = [self._games[item_id] for item_id in selected if item_id in self._games]
        self._start_tests(games)

    def _test_all(self) -> None:
        games = list(self._games.values())
        if not games:
            messagebox.showinfo("Tester-Spin", "Primero cargá el catálogo.")
            return
        if not messagebox.askyesno("Tester-Spin", f"¿Probar los {len(games)} juegos del proveedor seleccionado?"):
            return
        self._start_tests(games)

    def _start_tests(self, games: list[Game]) -> None:
        if self._worker and self._worker.is_alive():
            return
        try:
            concurrency = max(1, int(self.concurrency_var.get()))
            repetitions = max(1, int(self.spins_var.get()))
            delay_s = max(0.0, float(self.delay_var.get()))
            timeout_s = max(1.0, float(self.timeout_var.get()))
        except ValueError:
            messagebox.showerror("Tester-Spin", "Revisá concurrencia, repeticiones, delay y timeout.")
            return

        provider = self._provider()
        self._stop_event = threading.Event()
        self._test_total = len(games)
        self._test_done = 0
        self._set_busy(True)
        self.status_var.set(f"Probando 0/{len(games)}...")
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=max(1, len(games)), value=0)
        self._append_log(
            f"=== INICIO: proveedor={provider.display_name}, juegos={len(games)}, "
            f"simultáneos={concurrency}, repeticiones/modo={repetitions}, delay={delay_s}s ==="
        )

        def on_result(result: GameTestResult) -> None:
            self.storage.record_result(result)
            self._events.put(("test_result", result))

        def worker() -> None:
            try:
                run_game_tests(
                    provider,
                    games,
                    concurrency=concurrency,
                    spins_per_game=repetitions,
                    delay_between_starts_s=delay_s,
                    timeout_s=timeout_s,
                    stop_event=self._stop_event,
                    progress=lambda message: self._events.put(("log", message)),
                    on_result=on_result,
                )
                self._events.put(("tests_done", None))
            except Exception as exc:
                self._events.put(("error", f"Pruebas: {type(exc).__name__}: {exc}"))

        self._worker = threading.Thread(target=worker, daemon=True, name="game-test-scheduler")
        self._worker.start()

    def _stop(self) -> None:
        self._stop_event.set()
        self.status_var.set("Deteniendo...")
        self.stop_btn.configure(state="disabled")

    def _refresh_games(self) -> None:
        if not self.provider_var.get() or self.provider_var.get() not in self._display_to_key:
            return
        provider = self._provider()
        games = self.storage.list_games(provider.key)
        self._games.clear()
        self._thumb_images.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)

        for game in games:
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
            self.tree.insert("", "end", iid=iid, text="", image=image, values=values)
        self.count_var.set(f"{len(games)} juegos")

    def _load_tree_thumbnail(self, path_text: str):
        if not path_text:
            return ""
        path = Path(path_text)
        if not path.exists():
            return ""
        key = str(path)
        if key in self._thumb_images:
            return self._thumb_images[key]
        try:
            with Image.open(path) as original:
                image = original.copy()
            image.thumbnail((96, 64), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
            self._thumb_images[key] = photo
            return photo
        except Exception:
            return ""

    def _show_selected_game(self) -> None:
        selection = self.tree.selection()
        if not selection:
            self.selection_var.set("Sin selección")
            return
        game = self._games.get(selection[0])
        if game is None:
            return
        self.selection_var.set(game.name)

        text = (
            f"Proveedor: {game.provider}\n"
            f"ID interno: {game.symbol or 'sin resolver'}\n"
            f"Slug: {game.slug}\n"
            f"Estado: {game.last_status}\n"
            f"Última prueba: {game.last_test_at or '—'}\n"
            f"Error: {game.last_error or '—'}\n\n"
            f"Página:\n{game.url}\n\n"
            f"Miniatura original:\n{game.thumbnail_url or '—'}\n\n"
            f"Archivo:\n{game.thumbnail_path or '—'}"
        )
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state="disabled")

        self._preview_image = None
        if game.thumbnail_path and Path(game.thumbnail_path).exists():
            try:
                with Image.open(game.thumbnail_path) as original:
                    image = original.copy()
                image.thumbnail((420, 270), Image.Resampling.LANCZOS)
                self._preview_image = ImageTk.PhotoImage(image)
                self.preview_label.configure(image=self._preview_image, text="")
                return
            except Exception:
                pass
        self.preview_label.configure(image="", text="Sin miniatura")

    def _open_selected_url(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        game = self._games.get(selection[0])
        if game:
            webbrowser.open(game.url)

    def _open_selected_folder(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        game = self._games.get(selection[0])
        if not game:
            return
        provider = self._provider()
        path = provider.game_dir(game) if hasattr(provider, "game_dir") else self.data_root
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _drain_events(self) -> None:
        while True:
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self._append_log(str(value))
            elif kind == "catalog_done":
                self.progress.stop()
                self.progress.configure(mode="determinate", maximum=100, value=100)
                self._set_busy(False)
                self.status_var.set(f"Catálogo actualizado: {value} juegos")
                self._refresh_games()
            elif kind == "test_result":
                result = value
                if isinstance(result, GameTestResult):
                    self._test_done += 1
                    self.progress.configure(value=self._test_done)
                    self.status_var.set(f"Probando {self._test_done}/{self._test_total}...")
                    warning_count = sum(1 for attempt in result.attempts if attempt.warning)
                    self._append_log(
                        f"[{result.game_name}] {result.status}: "
                        f"{result.successful_spins}/{result.requested_spins} intentos de modo OK"
                        + (f", warnings={warning_count}" if warning_count else "")
                    )
                    self._refresh_games()
            elif kind == "tests_done":
                self._set_busy(False)
                self.status_var.set(f"Pruebas terminadas: {self._test_done}/{self._test_total}")
                self.progress.configure(value=self._test_total)
                self._refresh_games()
            elif kind == "error":
                self.progress.stop()
                self._set_busy(False)
                self.status_var.set("Error")
                self._append_log(str(value))
                messagebox.showerror("Tester-Spin", str(value))

        self.after(100, self._drain_events)

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


def main() -> None:
    TesterSpinApp().mainloop()


if __name__ == "__main__":
    main()
