from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tester_spin.multi_provider_catalog import (  # noqa: E402
    DEFAULT_PROVIDER_KEYS,
    build_providers,
    run_provider_catalogs,
)
from tester_spin.storage import Storage  # noqa: E402


def parse_provider_keys(value: str) -> list[str]:
    text = str(value or "").strip().casefold()
    if text == "all":
        return list(DEFAULT_PROVIDER_KEYS)

    allowed = set(DEFAULT_PROVIDER_KEYS)
    result: list[str] = []
    seen: set[str] = set()
    for raw in text.split(","):
        key = raw.strip()
        if not key:
            continue
        if key not in allowed:
            valid = ", ".join(DEFAULT_PROVIDER_KEYS)
            raise ValueError(
                f"Proveedor no habilitado para batch: {key!r}. Válidos: {valid}"
            )
        if key in seen:
            continue
        seen.add(key)
        result.append(key)

    if not result:
        raise ValueError("Debe seleccionar al menos un proveedor.")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Crawler headless multi-proveedor. Ejecuta proveedores en paralelo, "
            "manteniendo aislado el crawler interno de cada módulo."
        )
    )
    parser.add_argument(
        "--providers",
        default="all",
        help=(
            "Lista separada por comas o 'all'. Batch habilitado: "
            + ", ".join(DEFAULT_PROVIDER_KEYS)
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=len(DEFAULT_PROVIDER_KEYS),
        help="Cantidad máxima de proveedores simultáneos.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Máximo de cargas/páginas por proveedor. 0 = catálogo completo.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("data"),
        help="Directorio de datos compartido con Tester-Spin.",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="Ruta del resumen JSON. Por defecto: <data-root>/catalog-batch-summary.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        provider_keys = parse_provider_keys(args.providers)
        workers = max(1, int(args.workers))
        max_pages = int(args.max_pages)
        if max_pages < 0:
            raise ValueError("--max-pages debe ser 0 o un entero positivo.")
    except ValueError as exc:
        parser.error(str(exc))

    data_root = Path(args.data_root).expanduser().resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    storage = Storage(data_root / "tester-spin.sqlite3")
    providers = build_providers(provider_keys, data_root)

    print_lock = threading.Lock()

    def progress(provider: str, message: str) -> None:
        with print_lock:
            print(f"[{provider}] {message}", flush=True)

    print(
        "=== CATALOG BATCH ===\n"
        f"providers={','.join(provider_keys)}\n"
        f"workers={min(workers, len(provider_keys))}\n"
        f"max_pages={max_pages} ({'completo' if max_pages == 0 else 'limitado'})\n"
        f"data_root={data_root}",
        flush=True,
    )

    results = run_provider_catalogs(
        providers,
        storage=storage,
        workers=workers,
        max_pages=max_pages,
        progress=progress,
    )

    summary_path = (
        Path(args.summary).expanduser().resolve()
        if args.summary is not None
        else data_root / "catalog-batch-summary.json"
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "tester-spin-catalog-batch-v1",
        "providers": provider_keys,
        "workers": min(workers, len(provider_keys)),
        "max_pages": max_pages,
        "full_catalog": max_pages == 0,
        "results": [result.to_dict() for result in results],
    }
    summary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n=== RESUMEN ===", flush=True)
    for result in results:
        authority = "sí" if result.authoritative else "no"
        reconciled = "sí" if result.reconciled else "no"
        print(
            f"[{result.provider}] {result.status} | juegos={result.games} | "
            f"autoridad={authority} | reconciliado={reconciled} | "
            f"eliminados={result.removed} | {result.elapsed_s:.2f}s",
            flush=True,
        )
        detail = result.error or (
            result.authority_reason if not result.authoritative else ""
        )
        if detail:
            print(f"[{result.provider}] detalle: {detail}", flush=True)

    print(f"Resumen JSON: {summary_path}", flush=True)
    return 0 if all(result.status == "OK" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
