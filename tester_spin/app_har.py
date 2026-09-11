from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tkinter import messagebox, ttk

from tester_spin.app_current import CurrentTesterSpinApp
from tester_spin.models import Game
from tester_spin.providers import RedTigerProvider, RubyPlayProvider


def _open_directory(path: Path) -> None:
    target = str(Path(path).resolve())
    if os.name == "nt":
        os.startfile(target)  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", target])
        return
    subprocess.Popen(["xdg-open", target])


def _find_button_by_text(root: Any, text: str):
    for child in root.winfo_children():
        try:
            if isinstance(child, ttk.Button) and str(child.cget("text")) == text:
                return child
        except Exception:
            pass
        found = _find_button_by_text(child, text)
        if found is not None:
            return found
    return None


class HARToolTesterSpinApp(CurrentTesterSpinApp):
    """Current GUI plus direct access to per-game HAR diagnostics."""

    def __init__(self) -> None:
        super().__init__()
        # run.py enters through this application layer. New providers are only
        # registered here; their protocol implementations remain isolated under
        # providers/<name>/ and GUI/scheduler/storage stay protocol-neutral.
        for provider in (
            RubyPlayProvider(self.data_root),
            RedTigerProvider(self.data_root),
        ):
            self.registry.register(provider)
            self._display_to_key[provider.display_name] = provider.key
        self.provider_combo.configure(values=list(self._display_to_key))

    def _build(self) -> None:
        super()._build()

        folder_button = _find_button_by_text(self, "Abrir carpeta")
        if folder_button is None:
            return

        self.open_har_folder_btn = ttk.Button(
            folder_button.master,
            text="Abrir carpeta HAR",
            command=self._open_selected_har_folder,
        )
        self.open_har_folder_btn.pack(side="left", padx=(8, 0))

    def _selected_game_for_har(self) -> Game | None:
        selection = list(self.tree.selection())
        if not selection:
            return None
        focused = str(self.tree.focus() or "")
        iid = focused if focused in selection else selection[0]
        return self._games.get(iid)

    def _open_selected_har_folder(self) -> None:
        game = self._selected_game_for_har()
        if game is None:
            messagebox.showinfo(
                "Tester-Spin",
                "Seleccioná un juego para abrir su carpeta HAR.",
            )
            return

        try:
            provider = self.registry.get(game.provider)
            folder = provider.har_artifact_dir(game)
        except Exception as exc:
            self._append_log(
                f"[{game.name}] carpeta HAR ERROR: {type(exc).__name__}: {exc}"
            )
            messagebox.showerror(
                "Tester-Spin",
                f"No se pudo localizar la carpeta HAR.\n\n{type(exc).__name__}: {exc}",
            )
            return

        if folder is None or not Path(folder).is_dir():
            messagebox.showinfo(
                "Tester-Spin",
                (
                    "Este juego todavía no tiene un HAR ni un directorio de "
                    "diagnóstico preparado. Ejecutá la prueba una vez para que "
                    "la suite intente capturarlo."
                ),
            )
            return

        try:
            _open_directory(Path(folder))
            self._append_log(f"[{game.name}] abriendo carpeta HAR: {folder}")
        except Exception as exc:
            self._append_log(
                f"[{game.name}] abrir carpeta HAR ERROR: {type(exc).__name__}: {exc}"
            )
            messagebox.showerror(
                "Tester-Spin",
                f"No se pudo abrir la carpeta.\n\n{type(exc).__name__}: {exc}",
            )


def main() -> None:
    app = HARToolTesterSpinApp()
    app.mainloop()


__all__ = ["HARToolTesterSpinApp", "main"]
