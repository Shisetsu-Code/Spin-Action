from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable

from tester_spin.purchase_coverage import PURCHASE_FAILED, PURCHASE_UNKNOWN


SCHEMA = "tester-spin/purchase-retry-manifest/v1"
_RETRY_STATES = {PURCHASE_FAILED, PURCHASE_UNKNOWN}
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"\b(authorization|cookie|token|session(?:id)?|password|passwd|secret|api[_-]?key)\b"
    r"\s*[:=]\s*([^\s,;]+)",
    re.IGNORECASE,
)
_DEFAULT_RETRY_REASON = "Purchase coverage unresolved; inspect per-game purchase-coverage.json."


def _safe_text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _URL_RE.sub("<url>", text)
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(0, int(limit))]


def _option_is_complete(option: dict[str, Any]) -> bool:
    return bool(
        option.get("executable") is True
        and str(option.get("wire_contract_state") or "UNKNOWN").upper() == "PROVEN"
        and str(option.get("execution_state") or "UNKNOWN").upper() == "COMPLETE"
        and option.get("terminal") is True
    )


def _pending_option(option: dict[str, Any]) -> dict[str, Any]:
    return {
        "purchase_id": _safe_text(option.get("purchase_id"), limit=160) or "PURCHASE_UNKNOWN",
        "wire_contract_state": str(option.get("wire_contract_state") or "UNKNOWN").upper(),
        "execution_state": str(option.get("execution_state") or "UNKNOWN").upper(),
        "terminal": option.get("terminal") is True,
        "reason": _safe_text(option.get("reason")),
    }


def _entry_reason(
    coverage: dict[str, Any],
    raw_row: dict[str, Any],
    pending_options: list[dict[str, Any]],
) -> str:
    direct = _safe_text(coverage.get("reason") or raw_row.get("runtime_error"))
    if direct:
        return direct
    option_reasons: list[str] = []
    for option in pending_options:
        reason = _safe_text(option.get("reason"))
        if reason and reason not in option_reasons:
            option_reasons.append(reason)
    if option_reasons:
        return _safe_text("; ".join(option_reasons))
    return _DEFAULT_RETRY_REASON


def build_retry_manifest(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        coverage = raw_row.get("coverage")
        if not isinstance(coverage, dict):
            continue
        state = str(coverage.get("state") or PURCHASE_UNKNOWN).upper()
        if state not in _RETRY_STATES:
            continue

        game = raw_row.get("game")
        if not isinstance(game, dict):
            game = {}
        provider = _safe_text(
            game.get("provider") or coverage.get("provider"),
            limit=100,
        )
        slug = _safe_text(
            game.get("slug") or coverage.get("game_slug"),
            limit=200,
        )

        options = coverage.get("options")
        pending_options = [
            _pending_option(option)
            for option in options
            if isinstance(option, dict) and not _option_is_complete(option)
        ] if isinstance(options, list) else []

        entries.append(
            {
                "provider": provider,
                "slug": slug,
                "state": state,
                "reason": _entry_reason(coverage, raw_row, pending_options),
                "pending_options": pending_options,
            }
        )

    return {
        "schema": SCHEMA,
        "count": len(entries),
        "entries": entries,
    }


def rows_from_campaign_summary(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    provider_summaries = summary.get("provider_summaries")
    if not isinstance(provider_summaries, list):
        return rows
    for provider_summary in provider_summaries:
        if not isinstance(provider_summary, dict):
            continue
        results = provider_summary.get("results")
        if not isinstance(results, list):
            continue
        rows.extend(item for item in results if isinstance(item, dict))
    return rows


def provider_blockers_from_campaign_summary(summary: dict[str, Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    provider_summaries = summary.get("provider_summaries")
    if not isinstance(provider_summaries, list):
        return blockers
    for provider_summary in provider_summaries:
        if not isinstance(provider_summary, dict):
            continue
        catalog_error = _safe_text(provider_summary.get("catalog_error"))
        if not catalog_error:
            continue
        blockers.append(
            {
                "provider": _safe_text(provider_summary.get("provider"), limit=100),
                "state": PURCHASE_UNKNOWN,
                "reason": catalog_error,
            }
        )
    return blockers


def write_retry_manifest(output_dir: Path | str) -> dict[str, Any]:
    root = Path(output_dir)
    source_path = root / "purchase-campaign.json"
    summary = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise ValueError("purchase-campaign.json must contain a JSON object")

    manifest = build_retry_manifest(rows_from_campaign_summary(summary))
    aggregate = summary.get("aggregate")
    if not isinstance(aggregate, dict):
        aggregate = {}
    blockers = provider_blockers_from_campaign_summary(summary)
    manifest["provider_blocker_count"] = len(blockers)
    manifest["provider_blockers"] = blockers
    manifest["source_closed"] = summary.get("closed") is True
    manifest["source_overall_state"] = _safe_text(
        aggregate.get("overall_state"),
        limit=80,
    )

    target = root / "purchase-retry-manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Genera un manifest sanitizado para reintentar sólo compras pendientes."
    )
    parser.add_argument("--output-dir", default="purchase-results")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = write_retry_manifest(args.output_dir)
    print(
        "PURCHASE RETRY MANIFEST "
        f"pending_games={manifest.get('count', 0)} "
        f"provider_blockers={manifest.get('provider_blocker_count', 0)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
