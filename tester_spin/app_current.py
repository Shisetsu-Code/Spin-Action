from __future__ import annotations

import queue

from tkinter import messagebox

from tester_spin.app_live import LiveTesterSpinApp
from tester_spin.models import GameTestResult


class CurrentTesterSpinApp(LiveTesterSpinApp):
    """Current GUI with explicit response/completion accounting."""

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
