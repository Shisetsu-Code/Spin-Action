from __future__ import annotations

import argparse
import json
import re
import threading
from pathlib import Path
from typing import Any

from scripts.actions_provider_probe import (
    build_direct_game,
    enumerate_catalog_for_probe,
    provider_class_for,
    select_games,
)
from tester_spin.models import Game, GameTestResult
from tester_spin.purchase_coverage import (
    PURCHASE_COMPLETE,
    PURCHASE_UNKNOWN,
    aggregate_purchase_coverages,
    finalize_purchase_coverage,
)


DEFAULT_PURCHASE_PROVIDERS = (
    "pragmatic",
    "belatra",
    "1spin4win",
    "rubyplay",
    "redtiger",
)


def expand_provider_selection(value: str) -> list[str]:
    raw = str(value or "").strip().lower()
    if raw == "all":
        return list(DEFAULT_PURCHASE_PROVIDERS)
    selected: list[str] = []
    for part in raw.split(","):
        key = part.strip().lower()
        if not key:
            continue
        if key == "one_spin4win":
            key = "1spin4win"
        if key not in DEFAULT_PURCHASE_PROVIDERS:
            raise ValueError(
                f"Proveedor no habilitado para compra: {part!r}. "
                f"Permitidos: {', '.join(DEFAULT_PURCHASE_PROVIDERS)}; BGaming está excluido."
            )
        if key not in selected:
            selected.append(key)
    if not selected:
        raise ValueError("Debe seleccionarse al menos un proveedor.")
    return selected


def _safe_component(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._")
    return clean[:160] or "unknown"


def _game_dict(game: Game) -> dict[str, Any]:
    return {
        "provider": game.provider,
        "slug": game.slug,
        "name": game.name,
        "url": game.url,
        "symbol": game.symbol,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _error_result(game: Game, exc: BaseException) -> GameTestResult:
    return GameTestResult(
        provider=game.provider,
        slug=game.slug,
        game_name=game.name,
        game_url=game.url,
        requested_spins=1,
        successful_spins=0,
        failed_spins=1,
        status="ERROR",
        symbol=game.symbol,
        error=f"{type(exc).__name__}: {exc}",
    )


def _unknown_coverage(result: GameTestResult, reason: str) -> dict[str, Any]:
    return finalize_purchase_coverage(
        result,
        options=[],
        inventory_state="UNKNOWN",
        authority="purchase-campaign:runtime-before-authoritative-inventory",
        no_purchase_proven=False,
        reason=str(reason or "Runtime failed before authoritative purchase inventory was available."),
    )


def run_selected_games(
    provider,
    games: list[Game],
    *,
    timeout_s: float,
    stop_event: threading.Event,
    output_dir: Path,
    progress,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    provider_root = Path(output_dir) / _safe_component(provider.key)

    for game in games:
        if stop_event.is_set():
            break
        prefix = f"[{provider.key}/{game.slug}]"

        def game_progress(message: str) -> None:
            progress(f"{prefix} {message}")

        runtime_exc: BaseException | None = None
        try:
            # Purchase paths deliberately bypass the general result finalizer.
            # Sampling quotas, full branch matrices and farm-readiness are a
            # different audit and must not turn a proven purchase into a false
            # negative (or promote an unproven one).
            result = provider.test_purchase_paths(
                game,
                timeout_s=max(1.0, float(timeout_s)),
                stop_event=stop_event,
                progress=game_progress,
            )
        except BaseException as exc:
            runtime_exc = exc
            result = _error_result(game, exc)
            game_progress(f"runtime ERROR: {result.error}")

        if runtime_exc is not None or str(result.status or "").upper() in {"ERROR", "CANCELADO", "CANCELLED"}:
            coverage = _unknown_coverage(
                result,
                result.error or "Runtime stopped before purchase inventory could be trusted.",
            )
        else:
            try:
                coverage = provider.build_purchase_coverage(game, result)
                if not isinstance(coverage, dict):
                    raise TypeError("build_purchase_coverage() no devolvió un objeto")
            except BaseException as exc:
                coverage = _unknown_coverage(
                    result,
                    f"Purchase coverage adapter failed: {type(exc).__name__}: {exc}",
                )
                game_progress(
                    f"clasificación de compras ERROR: {type(exc).__name__}: {exc}"
                )

        target = provider_root / _safe_component(game.slug)
        _write_json(target / "result.json", result.to_dict())
        _write_json(target / "purchase-coverage.json", coverage)

        state = str(coverage.get("state") or PURCHASE_UNKNOWN)
        counts = coverage.get("counts") if isinstance(coverage.get("counts"), dict) else {}
        game_progress(
            "PURCHASE "
            f"state={state} total={counts.get('total', 0)} "
            f"complete={counts.get('complete', 0)} "
            f"failed={counts.get('failed', 0)} unknown={counts.get('unknown', 0)}"
        )
        rows.append(
            {
                "game": _game_dict(game),
                "runtime_status": result.status,
                "runtime_error": result.error,
                "run_dir": result.run_dir,
                "coverage": coverage,
            }
        )

    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Valida compras de proveedor con evidencia wire exacta y fail-closed."
    )
    parser.add_argument(
        "--provider",
        default="all",
        help="Proveedor, CSV de proveedores o 'all'. BGaming está excluido.",
    )
    parser.add_argument("--slug", default="")
    parser.add_argument("--game-url", default="")
    parser.add_argument("--game-name", default="")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--game-offset", type=int, default=0)
    parser.add_argument("--game-limit", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="0 = catálogo completo por el enumerador low-traffic.",
    )
    parser.add_argument("--data-root", default="purchase-results/data")
    parser.add_argument("--output-dir", default="purchase-results")
    return parser


def _select_provider_games(provider, args: argparse.Namespace, stop_event, progress) -> tuple[list[Game], dict[str, Any]]:
    direct_target = bool(str(args.game_url or "").strip())
    if direct_target:
        game = build_direct_game(
            provider_key=provider.key,
            slug=args.slug,
            name=args.game_name,
            url=args.game_url,
            symbol=args.symbol,
        )
        return [game], {
            "mode": "direct-target",
            "count": 1,
            "authoritative": False,
            "authority_reason": "catalog crawl intentionally skipped",
        }

    provider.set_catalog_authority(True, "")
    games = enumerate_catalog_for_probe(
        provider,
        requested_pages=max(0, int(args.max_pages)),
        stop_event=stop_event,
        progress=progress,
    )
    selected = select_games(
        games,
        slug=args.slug,
        offset=max(0, int(args.game_offset)),
        limit=int(args.game_limit),
    )
    return selected, {
        "mode": "crawl-low-traffic",
        "count": len(games),
        "selected_count": len(selected),
        "authoritative": bool(provider.catalog_crawl_authoritative),
        "authority_reason": str(provider.catalog_crawl_reason or ""),
    }


def run_campaign(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    providers = expand_provider_selection(args.provider)
    output_dir = Path(args.output_dir).resolve()
    data_root = Path(args.data_root).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)
    stop_event = threading.Event()
    log_lines: list[str] = []

    def progress(message: str) -> None:
        clean = str(message or "").rstrip()
        if clean:
            print(clean, flush=True)
            log_lines.append(clean)

    all_rows: list[dict[str, Any]] = []
    provider_summaries: list[dict[str, Any]] = []

    for key in providers:
        provider = provider_class_for(key)(data_root)
        try:
            selected, catalog = _select_provider_games(provider, args, stop_event, progress)
        except BaseException as exc:
            provider_summaries.append(
                {
                    "provider": key,
                    "selected_count": 0,
                    "aggregate": {
                        "overall_state": PURCHASE_UNKNOWN,
                        "closed": False,
                        "reason": f"catalog error: {type(exc).__name__}: {exc}",
                    },
                    "catalog_error": f"{type(exc).__name__}: {exc}",
                }
            )
            progress(f"[{key}] catálogo ERROR: {type(exc).__name__}: {exc}")
            continue

        progress(f"[{key}] selected={len(selected)} catalog={catalog.get('count', 0)}")
        rows = run_selected_games(
            provider,
            selected,
            timeout_s=max(1.0, float(args.timeout)),
            stop_event=stop_event,
            output_dir=output_dir,
            progress=progress,
        )
        coverages = [row["coverage"] for row in rows]
        aggregate = aggregate_purchase_coverages(coverages)
        if not selected:
            aggregate["overall_state"] = PURCHASE_UNKNOWN
            aggregate["closed"] = False
            aggregate["reason"] = "No games selected; purchase coverage cannot close."
        provider_summaries.append(
            {
                "provider": key,
                "catalog": catalog,
                "selected_count": len(selected),
                "aggregate": aggregate,
                "results": rows,
            }
        )
        all_rows.extend(rows)

    global_aggregate = aggregate_purchase_coverages(
        [row["coverage"] for row in all_rows]
    )
    provider_closed = bool(provider_summaries) and all(
        bool(summary.get("aggregate", {}).get("closed"))
        for summary in provider_summaries
    )
    selected_count = sum(int(summary.get("selected_count") or 0) for summary in provider_summaries)
    closed = bool(provider_closed and selected_count > 0)
    if not closed:
        global_aggregate["closed"] = False
        if global_aggregate.get("overall_state") == PURCHASE_COMPLETE:
            global_aggregate["overall_state"] = PURCHASE_UNKNOWN

    summary = {
        "schema": "tester-spin/purchase-campaign/v1",
        "providers": providers,
        "settings": {
            "slug": str(args.slug or ""),
            "game_url": str(args.game_url or ""),
            "game_name": str(args.game_name or ""),
            "symbol": str(args.symbol or ""),
            "game_offset": int(args.game_offset),
            "game_limit": int(args.game_limit),
            "timeout": float(args.timeout),
            "max_pages": int(args.max_pages),
        },
        "selected_count": selected_count,
        "aggregate": global_aggregate,
        "provider_summaries": provider_summaries,
        "closed": closed,
    }
    _write_json(output_dir / "purchase-campaign.json", summary)
    (output_dir / "purchase-campaign.log").write_text(
        "\n".join(log_lines) + ("\n" if log_lines else ""),
        encoding="utf-8",
    )
    progress(
        f"PURCHASE CAMPAIGN closed={closed} overall={global_aggregate.get('overall_state')} "
        f"selected={selected_count}"
    )
    return closed, summary


def main() -> int:
    args = build_parser().parse_args()
    try:
        closed, _summary = run_campaign(args)
    except BaseException as exc:
        output_dir = Path(getattr(args, "output_dir", "purchase-results")).resolve()
        _write_json(
            output_dir / "purchase-campaign.json",
            {
                "schema": "tester-spin/purchase-campaign/v1",
                "closed": False,
                "aggregate": {"overall_state": PURCHASE_UNKNOWN, "closed": False},
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )
        print(f"PURCHASE CAMPAIGN ERROR: {type(exc).__name__}: {exc}", flush=True)
        return 2
    return 0 if closed else 2


if __name__ == "__main__":
    raise SystemExit(main())
