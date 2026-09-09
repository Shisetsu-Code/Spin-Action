from __future__ import annotations

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
    - per-game game.json metadata used by Pragmatic fallback recovery;
    - per-game thumbnail.* files.

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
            game_json.unlink()
            files_removed += 1

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
