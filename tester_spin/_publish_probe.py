from __future__ import annotations

import subprocess
from pathlib import Path


def push_results(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "push", "origin", "HEAD:tester-spin-runs"], check=True)
