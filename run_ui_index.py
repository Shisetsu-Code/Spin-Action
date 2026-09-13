from __future__ import annotations

import run as _base_run
from tester_spin.providers.bgaming_ui_index_bridge import (
    install_bgaming_ui_index_bridge,
)


def main() -> int:
    install_bgaming_ui_index_bridge()
    return _base_run.main()


if __name__ == "__main__":
    raise SystemExit(main())
