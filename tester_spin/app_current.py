from __future__ import annotations

import queue

from tkinter import messagebox, ttk

from tester_spin.app_live import LiveTesterSpinApp
from tester_spin.models import Game, GameTestResult
from tester_spin.ui_game_index import game_sort_key, retryable_games


class CurrentTesterSpinApp(LiveTesterSpinApp):
    """Current GUI with live persisted status indexing and targeted retry actions."""

    _SORT_LABELS = {
        "name": "Nombre",
        "symbol": "ID proveedor",
        "status": "Estado",
        "tested": "Última prueba",
        "url": "Link",
    }

    def __init__(self) -> None:
        self._sort_column = "name"
        self._sort_reverse = False
        super().__init__()

    def _build(self) -> None:
        super()._build()

        # The base table previously rendered headings as inert labels. Attach a
        # real sort command to every data column and show the active direction.
        for column, label in self._SORT_LABELS.items():
            self.tree.heading(
                column,
                text=label,
                command=lambda selected=column: self._sort_by(selected),
            )
        self._update_sort_headings()

        # Reuse the same controls row as PROBAR TODOS so the action is visible and
        # obeys the same busy/disabled lifecycle.
        controls = self.test_all_btn.master
        self.test_non_ok_btn = ttk.Button(
            controls,
            text="PROBAR NO OK",
            command=self._test_non_ok,
        )
        self.test_non_ok_btn.pack(side="left", padx=(8, 0))

    def _set_busy(self, busy: bool) -> None:
        super()._set_busy(busy)
        if hasattr(self, "test_non_ok_btn"):
            self.test_non_ok_btn.configure(state="disabled" if busy else "normal")

    def _test_non_ok(self) -> None:
        provider = self._provider()
        # Read from SQLite at click time instead of trusting potentially stale
        # in-memory rows. PENDIENTE, PARCIAL and ERROR are all retryable.
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

    def _sort_by(self, column: str) -> None:
        if column == self._sort_column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            # The useful first view for timestamps is newest first. Other columns
            # start ascending; Estado therefore starts ERROR/PARCIAL/PENDIENTE/OK.
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
            # Missing timestamps always stay at the bottom, including when newest
            # first is selected.
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
        while True:
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                break

            if kind == "catalog_game":
                provider_key, slug = value  # type: ignore[misc]
                self._upsert_catalog_row(str(provider_key), str(slug))
            elif kind == "log":
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

                    responded = sum(1 for attempt in result.attempts if attempt.ok)
                    completed = sum(
                        1 for attempt in result.attempts if attempt.ok and attempt.terminal
                    )
                    pending = sum(
                        1 for attempt in result.attempts if attempt.ok and not attempt.terminal
                    )
                    errors = sum(1 for attempt in result.attempts if not attempt.ok)
                    missing = max(0, result.requested_spins - len(result.attempts))
                    errors += missing

                    self._append_log(
                        f"[{result.game_name}] {result.status}: "
                        f"respondieron={responded}/{result.requested_spins}, "
                        f"completados={completed}/{result.requested_spins}, "
                        f"pendientes={pending}, errores={errors}"
                    )
                    # record_result() has already committed the new state in the
                    # worker callback, so refreshing here immediately updates both
                    # Estado and Última prueba from the authoritative database.
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


def main() -> None:
    CurrentTesterSpinApp().mainloop()


if __name__ == "__main__":
    main()
