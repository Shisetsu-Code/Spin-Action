from __future__ import annotations

import queue
import threading
import time

from tkinter import messagebox, ttk

from tester_spin.app_live import LiveTesterSpinApp
from tester_spin.execution_backend import ExecutionConfig, LocalThreadExecutionBackend
from tester_spin.models import Game, GameTestResult
from tester_spin.ui_game_index import game_sort_key, retryable_games


class CurrentTesterSpinApp(LiveTesterSpinApp):
    """Current GUI with responsive event batching and pluggable execution backend."""

    _SORT_LABELS = {
        "name": "Nombre",
        "symbol": "ID proveedor",
        "status": "Estado",
        "tested": "Última prueba",
        "url": "Link",
    }
    _MAX_EVENTS_PER_TICK = 160
    _UI_DRAIN_BUDGET_S = 0.012

    def __init__(self) -> None:
        self._sort_column = "name"
        self._sort_reverse = False
        self._execution_backend = LocalThreadExecutionBackend()
        super().__init__()

    def _build(self) -> None:
        super()._build()

        for column, label in self._SORT_LABELS.items():
            self.tree.heading(
                column,
                text=label,
                command=lambda selected=column: self._sort_by(selected),
            )
        self._update_sort_headings()

        controls = self.test_all_btn.master
        self.test_non_ok_btn = ttk.Button(
            controls,
            text="PROBAR NO OK",
            command=self._test_non_ok,
        )
        self.test_non_ok_btn.pack(side="left", padx=(8, 0))
        ttk.Label(
            controls,
            text=f"Ejecución: {self._execution_backend.display_name}",
        ).pack(side="left", padx=(14, 0))

    def _set_busy(self, busy: bool) -> None:
        super()._set_busy(busy)
        if hasattr(self, "test_non_ok_btn"):
            self.test_non_ok_btn.configure(state="disabled" if busy else "normal")

    def _test_non_ok(self) -> None:
        provider = self._provider()
        games = retryable_games(self.storage.list_games(provider.key))
        if not games:
            messagebox.showinfo("Tester-Spin", "No hay juegos pendientes: todos están OK.")
            return

        counts: dict[str, int] = {}
        for game in games:
            status = str(game.last_status or "PENDIENTE").strip().upper() or "PENDIENTE"
            counts[status] = counts.get(status, 0) + 1
        detail = ", ".join(f"{key}={counts[key]}" for key in sorted(counts))
        if not messagebox.askyesno(
            "Tester-Spin",
            f"¿Probar los {len(games)} juegos que no están OK?\n\n{detail}",
        ):
            return
        self._start_tests(games)

    def _start_tests(self, games: list[Game]) -> None:
        """Start one orchestration thread; the backend owns the worker pool.

        Tk never performs provider I/O, waits on futures, or writes result rows.
        This boundary is intentionally transport-neutral so a remote execution
        backend can later replace LocalThreadExecutionBackend.
        """
        if self._worker and self._worker.is_alive():
            return
        try:
            config = ExecutionConfig(
                concurrency=max(1, int(self.concurrency_var.get())),
                spins_per_game=max(1, int(self.spins_var.get())),
                delay_between_starts_s=max(0.0, float(self.delay_var.get())),
                timeout_s=max(1.0, float(self.timeout_var.get())),
            )
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
            f"=== INICIO: proveedor={provider.display_name}, backend={self._execution_backend.key}, "
            f"juegos={len(games)}, simultáneos={config.concurrency}, "
            f"repeticiones/modo={config.spins_per_game}, "
            f"delay={config.delay_between_starts_s}s ==="
        )

        def on_result(result: GameTestResult) -> None:
            # Persistence is intentionally outside Tk's main thread.
            self.storage.record_result(result)
            self._events.put(("test_result", result))

        def orchestrator() -> None:
            try:
                self._execution_backend.run(
                    provider,
                    games,
                    config=config,
                    stop_event=self._stop_event,
                    progress=lambda message: self._events.put(("log", message)),
                    on_result=on_result,
                )
                self._events.put(("tests_done", None))
            except Exception as exc:
                self._events.put(("error", f"Pruebas: {type(exc).__name__}: {exc}"))

        self._worker = threading.Thread(
            target=orchestrator,
            daemon=True,
            name="execution-orchestrator",
        )
        self._worker.start()

    def _sort_by(self, column: str) -> None:
        if column == self._sort_column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = column == "tested"
        self._apply_current_sort()
        self._update_sort_headings()

    def _update_sort_headings(self) -> None:
        for column, label in self._SORT_LABELS.items():
            suffix = ""
            if column == self._sort_column:
                suffix = " ▼" if self._sort_reverse else " ▲"
            self.tree.heading(
                column,
                text=label + suffix,
                command=lambda selected=column: self._sort_by(selected),
            )

    def _apply_current_sort(self) -> None:
        rows: list[tuple[str, Game]] = []
        for iid in self.tree.get_children(""):
            game = self._games.get(iid)
            if game is not None:
                rows.append((iid, game))

        if self._sort_column == "tested":
            tested = [(iid, game) for iid, game in rows if str(game.last_test_at or "").strip()]
            untested = [(iid, game) for iid, game in rows if not str(game.last_test_at or "").strip()]
            tested.sort(
                key=lambda item: game_sort_key(item[1], "tested"),
                reverse=self._sort_reverse,
            )
            untested.sort(key=lambda item: game_sort_key(item[1], "name"))
            ordered = tested + untested
        else:
            ordered = sorted(
                rows,
                key=lambda item: game_sort_key(item[1], self._sort_column),
                reverse=self._sort_reverse,
            )

        for position, (iid, _game) in enumerate(ordered):
            self.tree.move(iid, "", position)

    def _update_count_summary(self) -> None:
        counts = {"OK": 0, "PARCIAL": 0, "ERROR": 0, "PENDIENTE": 0}
        for game in self._games.values():
            status = str(game.last_status or "PENDIENTE").strip().upper() or "PENDIENTE"
            if status not in counts:
                status = "PENDIENTE"
            counts[status] += 1
        self.count_var.set(
            f"{len(self._games)} juegos | OK {counts['OK']} | PARCIAL {counts['PARCIAL']} | "
            f"ERROR {counts['ERROR']} | PENDIENTE {counts['PENDIENTE']}"
        )

    def _refresh_games(self) -> None:
        super()._refresh_games()
        self._apply_current_sort()
        self._update_sort_headings()
        self._update_count_summary()

    def _upsert_catalog_row(self, provider_key: str, slug: str) -> None:
        super()._upsert_catalog_row(provider_key, slug)
        self._apply_current_sort()
        self._update_count_summary()

    def _drain_events(self) -> None:
        """Drain with a hard item/time budget so Tk always regains control.

        Worker throughput can be much higher than UI rendering throughput, especially
        once execution is remote.  Never empty an unbounded producer queue in one Tk
        callback.  Batch text writes and table re-sorts once per UI slice.
        """
        started = time.monotonic()
        processed = 0
        log_lines: list[str] = []
        table_dirty = False
        full_refresh = False

        while processed < self._MAX_EVENTS_PER_TICK:
            if time.monotonic() - started >= self._UI_DRAIN_BUDGET_S:
                break
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                break
            processed += 1

            if kind == "catalog_game":
                provider_key, slug = value  # type: ignore[misc]
                # Do the single-row update, but postpone sort/count until the end
                # of this UI slice rather than doing O(n) work per catalog event.
                LiveTesterSpinApp._upsert_catalog_row(self, str(provider_key), str(slug))
                table_dirty = True
            elif kind == "log":
                log_lines.append(str(value))
            elif kind == "catalog_done":
                self.progress.stop()
                self.progress.configure(mode="determinate", maximum=100, value=100)
                self._set_busy(False)
                self.status_var.set(f"Catálogo actualizado: {value} juegos")
                full_refresh = True
            elif kind == "test_result":
                result = value
                if isinstance(result, GameTestResult):
                    self._test_done += 1
                    self.progress.configure(value=self._test_done)
                    self.status_var.set(f"Probando {self._test_done}/{self._test_total}...")

                    responded = sum(1 for attempt in result.attempts if attempt.ok)
                    completed = sum(1 for attempt in result.attempts if attempt.ok and attempt.terminal)
                    pending = sum(1 for attempt in result.attempts if attempt.ok and not attempt.terminal)
                    errors = sum(1 for attempt in result.attempts if not attempt.ok)
                    errors += max(0, result.requested_spins - len(result.attempts))
                    log_lines.append(
                        f"[{result.game_name}] {result.status}: "
                        f"respondieron={responded}/{result.requested_spins}, "
                        f"completados={completed}/{result.requested_spins}, "
                        f"pendientes={pending}, errores={errors}"
                    )

                    # record_result() already committed in the orchestrator thread.
                    # Refresh only this row instead of rebuilding all ~642 rows.
                    LiveTesterSpinApp._upsert_catalog_row(self, result.provider, result.slug)
                    table_dirty = True
            elif kind == "tests_done":
                self._set_busy(False)
                self.status_var.set(f"Pruebas terminadas: {self._test_done}/{self._test_total}")
                self.progress.configure(value=self._test_total)
                full_refresh = True
            elif kind == "error":
                self.progress.stop()
                self._set_busy(False)
                self.status_var.set("Error")
                log_lines.append(str(value))
                messagebox.showerror("Tester-Spin", str(value))

        if log_lines:
            self._append_log("\n".join(log_lines))

        if full_refresh:
            self._refresh_games()
        elif table_dirty:
            self._apply_current_sort()
            self._update_sort_headings()
            self._update_count_summary()

        # Backlogged remote/local producers get fast incremental draining; when the
        # queue is quiet we reduce idle wakeups.
        self.after(15 if not self._events.empty() else 75, self._drain_events)


def main() -> None:
    CurrentTesterSpinApp().mainloop()


if __name__ == "__main__":
    main()
