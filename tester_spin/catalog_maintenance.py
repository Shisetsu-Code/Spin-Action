from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CatalogPurgeStats:
    files_removed: int = 0
    directories_removed: int = 0


def purge_provider_catalog_artifacts(provider_root: Path) -> CatalogPurgeStats:
    """Remove catalogue-derived artifacts without deleting test/runtime evidence.

    The reset intentionally removes:
    - provider-level catalog.json;
    - catalog-pages/ and catalog-diagnostics/;
    - per-game thumbnail.* files.

    Per-game game.json is preserved because it may contain provider protocol metadata
    (symbol/cver/endpoint/mode catalogue). Instead it is marked with
    catalog_recovery_disabled=true so Pragmatic fallback recovery cannot resurrect
    the cleared catalogue until a fresh live crawl sees the game again.

    A per-game tests/ directory and any other runtime artifacts are preserved.
    """
    root = Path(provider_root)
    if not root.exists():
        return CatalogPurgeStats()

    files_removed = 0
    directories_removed = 0

    catalog_json = root / "catalog.json"
    if catalog_json.is_file():
        catalog_json.unlink()
        files_removed += 1

    for name in ("catalog-pages", "catalog-diagnostics"):
        path = root / name
        if path.is_dir():
            shutil.rmtree(path)
            directories_removed += 1

    for child in list(root.iterdir()):
        if not child.is_dir():
            continue

        game_json = child / "game.json"
        if game_json.is_file():
            try:
                metadata = json.loads(game_json.read_text(encoding="utf-8"))
                if isinstance(metadata, dict):
                    metadata["catalog_recovery_disabled"] = True
                    game_json.write_text(
                        json.dumps(metadata, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            except Exception:
                # If legacy metadata cannot be parsed, leave it untouched rather
                # than destroying potentially useful runtime evidence.
                pass

        for thumbnail in child.glob("thumbnail.*"):
            if thumbnail.is_file():
                thumbnail.unlink()
                files_removed += 1

        try:
            child.rmdir()
            directories_removed += 1
        except OSError:
            # Non-empty directories (notably tests/) are deliberately preserved.
            pass

    return CatalogPurgeStats(
        files_removed=files_removed,
        directories_removed=directories_removed,
    )
