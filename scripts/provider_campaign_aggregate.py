from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


def _slug_from_coverage(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    game = row.get("game")
    if isinstance(game, dict):
        return str(game.get("slug") or "").strip()
    return str(row.get("slug") or "").strip()


def _slug_from_natural(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    game = row.get("game")
    if isinstance(game, dict) and game.get("slug"):
        return str(game.get("slug") or "").strip()
    return str(row.get("slug") or "").strip()


def _index_rows(
    rows: Iterable[Any],
    *,
    slug_reader,
    catalog_slugs: set[str],
) -> tuple[dict[str, dict[str, Any]], list[str], list[str], list[str]]:
    clean_rows = [row for row in rows if isinstance(row, dict)]
    slugs = [slug_reader(row) for row in clean_rows]
    invalid = sorted({slug for slug in slugs if not slug})
    counts = Counter(slug for slug in slugs if slug)
    duplicates = sorted(slug for slug, count in counts.items() if count > 1)
    extras = sorted(set(counts) - catalog_slugs)
    missing = sorted(catalog_slugs - set(counts))
    indexed: dict[str, dict[str, Any]] = {}
    for row, slug in zip(clean_rows, slugs):
        if slug and slug not in indexed:
            indexed[slug] = row
    if invalid:
        extras.append("<empty-slug>")
    return indexed, missing, duplicates, sorted(set(extras))


def aggregate_campaign(
    manifest: dict[str, Any],
    *,
    coverage_results: Iterable[dict[str, Any]],
    natural_results: Iterable[dict[str, Any]],
    required_natural_spins: int = 0,
) -> dict[str, Any]:
    """Aggregate a provider campaign without trusting shard/job exit status.

    COMPLETE is possible only when the catalog is explicitly authoritative,
    every catalog slug appears exactly once in coverage, every coverage verdict
    is COMPLETE, and (when requested) every slug has a fully successful natural
    soak meeting the configured spin quota.
    """

    catalog_errors: list[str] = []
    games = manifest.get("games") if isinstance(manifest, dict) else None
    if not isinstance(games, list):
        games = []
        catalog_errors.append("manifest.games is missing or not a list")

    catalog_slugs_list: list[str] = []
    for index, game in enumerate(games):
        if not isinstance(game, dict):
            catalog_errors.append(f"manifest.games[{index}] is not an object")
            continue
        slug = str(game.get("slug") or "").strip()
        if not slug:
            catalog_errors.append(f"manifest.games[{index}] has empty slug")
            continue
        catalog_slugs_list.append(slug)

    slug_counts = Counter(catalog_slugs_list)
    duplicate_catalog = sorted(slug for slug, count in slug_counts.items() if count > 1)
    if duplicate_catalog:
        catalog_errors.append("duplicate catalog slugs: " + ", ".join(duplicate_catalog))

    declared_count = manifest.get("count") if isinstance(manifest, dict) else None
    if declared_count is not None:
        try:
            parsed_count = int(declared_count)
        except (TypeError, ValueError):
            catalog_errors.append("manifest.count is not an integer")
        else:
            if parsed_count != len(games):
                catalog_errors.append(
                    f"manifest.count={parsed_count} but games={len(games)}"
                )

    authoritative = bool(manifest.get("authoritative")) if isinstance(manifest, dict) else False
    if not authoritative:
        catalog_errors.append(
            str(manifest.get("authority_reason") or "catalog is not authoritative")
            if isinstance(manifest, dict)
            else "catalog is not authoritative"
        )

    catalog_slugs = set(catalog_slugs_list)
    coverage_index, coverage_missing, coverage_duplicates, coverage_extras = _index_rows(
        coverage_results,
        slug_reader=_slug_from_coverage,
        catalog_slugs=catalog_slugs,
    )

    coverage_failed: dict[str, str] = {}
    for slug in sorted(catalog_slugs & set(coverage_index)):
        verdict = str(coverage_index[slug].get("audit_verdict") or "UNKNOWN").strip().upper()
        if verdict != "COMPLETE":
            coverage_failed[slug] = verdict or "UNKNOWN"

    required = max(0, int(required_natural_spins))
    natural_required = required > 0
    natural_index, natural_missing, natural_duplicates, natural_extras = _index_rows(
        natural_results,
        slug_reader=_slug_from_natural,
        catalog_slugs=catalog_slugs,
    )

    natural_failed: dict[str, dict[str, Any]] = {}
    if natural_required:
        for slug in sorted(catalog_slugs & set(natural_index)):
            row = natural_index[slug]
            status = str(row.get("status") or "").strip().upper()
            try:
                requested = int(row.get("requested_spins") or 0)
                successful = int(row.get("successful_spins") or 0)
                failed = int(row.get("failed_spins") or 0)
            except (TypeError, ValueError):
                requested = successful = 0
                failed = 1
            if (
                status != "OK"
                or requested < required
                or successful < required
                or failed != 0
            ):
                natural_failed[slug] = {
                    "status": status or "UNKNOWN",
                    "requested_spins": requested,
                    "successful_spins": successful,
                    "failed_spins": failed,
                    "required_spins": required,
                }
    else:
        natural_missing = []
        natural_duplicates = []
        natural_extras = []

    structural_errors = bool(
        duplicate_catalog
        or coverage_duplicates
        or coverage_extras
        or natural_duplicates
        or natural_extras
    )
    incomplete = bool(
        coverage_missing
        or coverage_failed
        or (natural_required and (natural_missing or natural_failed))
    )

    if structural_errors:
        overall = "ERROR"
    elif not authoritative or any(
        message.startswith("manifest.") or message.startswith("duplicate catalog")
        for message in catalog_errors
    ):
        overall = "UNKNOWN"
    elif incomplete:
        overall = "INCOMPLETE"
    else:
        overall = "COMPLETE"

    return {
        "schema": "tester-spin/provider-campaign-aggregate/v1",
        "provider": str(manifest.get("provider") or "") if isinstance(manifest, dict) else "",
        "overall_verdict": overall,
        "catalog_authoritative": authoritative,
        "catalog_errors": catalog_errors,
        "counts": {
            "catalog": len(catalog_slugs),
            "coverage_complete": len(catalog_slugs) - len(coverage_missing) - len(coverage_failed),
            "natural_complete": (
                len(catalog_slugs) - len(natural_missing) - len(natural_failed)
                if natural_required
                else 0
            ),
            "required_natural_spins": required,
        },
        "coverage": {
            "missing": coverage_missing,
            "duplicates": coverage_duplicates,
            "extras": coverage_extras,
            "failed": coverage_failed,
        },
        "natural": {
            "required": natural_required,
            "required_spins": required,
            "missing": natural_missing,
            "duplicates": natural_duplicates,
            "extras": natural_extras,
            "failed": natural_failed,
        },
    }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _rows_from_files(paths: list[Path], *, key: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        payload = _read_json(path)
        if isinstance(payload, list):
            rows.extend(row for row in payload if isinstance(row, dict))
            continue
        if isinstance(payload, dict):
            value = payload.get(key)
            if isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))
            else:
                rows.append(payload)
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate provider campaign artifacts fail-closed.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--coverage", action="append", default=[])
    parser.add_argument("--natural", action="append", default=[])
    parser.add_argument("--required-natural-spins", type=int, default=0)
    parser.add_argument("--output", default="campaign-summary.json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = _read_json(Path(args.manifest))
    if not isinstance(manifest, dict):
        raise SystemExit("manifest must be a JSON object")
    coverage = _rows_from_files([Path(value) for value in args.coverage], key="results")
    natural = _rows_from_files([Path(value) for value in args.natural], key="results")
    result = aggregate_campaign(
        manifest,
        coverage_results=coverage,
        natural_results=natural,
        required_natural_spins=args.required_natural_spins,
    )
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["overall_verdict"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
