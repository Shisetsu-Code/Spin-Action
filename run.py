from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _error_log_path() -> Path:
    home = os.environ.get("TESTER_SPIN_APP_HOME", "").strip()
    if home:
        return Path(home) / "app-startup-error.log"
    return Path.cwd() / "app-startup-error.log"


def _show_error(message: str) -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "Tester-Spin - Error", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def main() -> int:
    try:
        from tester_spin.app_current import main as app_main

        app_main()
        return 0
    except BaseException as exc:
        detail = traceback.format_exc()
        log_path = _error_log_path()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(detail, encoding="utf-8")
        except Exception:
            pass
        _show_error(
            f"Tester-Spin no pudo iniciar.\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            f"Diagnóstico: {log_path}"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
