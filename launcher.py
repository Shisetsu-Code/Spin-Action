from __future__ import annotations

import os
import subprocess
import sys
import traceback

from tester_spin.updater import Updater


def _message_box(title: str, message: str, *, error: bool = False) -> None:
    if os.name == "nt":
        try:
            import ctypes

            flags = 0x10 if error else 0x40
            ctypes.windll.user32.MessageBoxW(None, message, title, flags)
            return
        except Exception:
            pass
    stream = sys.stderr if error else sys.stdout
    print(f"{title}: {message}", file=stream)


class Splash:
    def __init__(self) -> None:
        self.root = None
        self.label = None
        try:
            import tkinter as tk
            from tkinter import ttk

            root = tk.Tk()
            root.title("Tester-Spin")
            root.geometry("470x130")
            root.resizable(False, False)
            root.attributes("-topmost", True)
            frame = ttk.Frame(root, padding=18)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text="Tester-Spin", font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
            label = ttk.Label(frame, text="Iniciando...", wraplength=430)
            label.pack(anchor="w", pady=(12, 0))
            root.update_idletasks()
            root.update()
            self.root = root
            self.label = label
        except Exception:
            self.root = None
            self.label = None

    def set(self, message: str) -> None:
        if self.root is None or self.label is None:
            return
        try:
            self.label.configure(text=message)
            self.root.update_idletasks()
            self.root.update()
        except Exception:
            self.root = None
            self.label = None

    def close(self) -> None:
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception:
                pass
        self.root = None
        self.label = None


def main() -> int:
    splash = Splash()
    try:
        updater = Updater()
        app_dir = updater.prepare_runtime(splash.set)
        python_exe, env = updater.ensure_environment(app_dir, splash.set)
        entrypoint = app_dir / "run.py"
        if not entrypoint.exists():
            raise RuntimeError(f"no existe {entrypoint}")

        # The managed Git checkout is disposable. Keep SQLite, RAW captures and
        # all provider artifacts in the configured persistent data directory by
        # making its parent the application cwd. Existing installs therefore keep
        # C:\Proyectos\Tester-Spin\data when that database is detected on first run.
        splash.set("Abriendo Tester-Spin...")
        subprocess.Popen(
            [str(python_exe), str(entrypoint)],
            cwd=str(updater.data_dir.parent),
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        splash.close()
        return 0
    except Exception as exc:
        splash.close()
        detail = f"{type(exc).__name__}: {exc}"
        log_path = None
        try:
            updater = locals().get("updater")
            if updater is not None:
                log_path = updater.home / "launcher-error.log"
                log_path.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        if log_path:
            detail += f"\n\nDiagnóstico: {log_path}"
        _message_box("Tester-Spin - Error de inicio", detail, error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
