from __future__ import annotations

import argparse
import json
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from scripts.actions_provider_probe import (
    build_direct_game,
    enumerate_catalog_for_probe,
    provider_class_for,
    select_games,
)
from tester_spin.models import Game, GameTestResult
from tester_spin.provider_rate_limit import ProviderRequestRateLimiter
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


def attach_safety_limiter(provider, *, requests_per_minute: int) -> dict[str, Any]:
    """Attach one provider-wide paced protocol request ceiling."""
    limiter = ProviderRequestRateLimiter(requests_per_minute=int(requests_per_minute))
    provider.set_request_rate_limiter(limiter)
    snapshot = provider.provider_request_rate_snapshot()
    return dict(snapshot) if isinstance(snapshot, dict) else limiter.snapshot()


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


def _runtime_failure_fingerprint(error: str) -> str:
    """Group repeated provider-level failures without depending on game identifiers."""
    text = str(error or "").strip().lower()
    markers = (
        ("http 403", "http-403"),
        ("403 client error", "http-403"),
        ("forbidden", "http-403"),
        ("cloudflare", "cloudflare"),
        ("http 429", "http-429"),
        ("429 client error", "http-429"),
        ("too many requests", "http-429"),
        ("http 503", "http-503"),
        ("503 server error", "http-503"),
        ("service unavailable", "http-503"),
        ("timed out", "timeout"),
        ("timeout", "timeout"),
        ("no demo", "no-demo"),
        ("sin demo", "no-demo"),
    )
    for marker, fingerprint in markers:
        if marker in text:
            return fingerprint
    text = re.sub(r"https?://\S+", "<url>", text)
    text = re.sub(r"\b[0-9a-f]{16,}\b", "<opaque>", text)
    text = re.sub(r"\b\d+\b", "<n>", text)
    text = re.sub(r"\s+", " ", text)
    return text[:240] or "unknown-runtime-error"


def _is_transport_blocking_failure(error: str) -> bool:
    """Return true only for repeatable network/edge failures worth throttling."""
    text = str(error or "").strip().lower()
    if not text:
        return False
    markers = (
        "http 403",
        "403 client error",
        "forbidden",
        "cloudflare",
        "http 429",
        "429 client error",
        "too many requests",
        "http 503",
        "503 server error",
        "service unavailable",
        "timed out",
        "timeout",
        "connection reset",
        "connection refused",
        "connection aborted",
        "name resolution",
        "dns",
        "sslerror",
        "ssl error",
    )
    return any(marker in text for marker in markers)


def _persist_row(
    provider_root: Path,
    game: Game,
    result: GameTestResult,
    coverage: dict[str, Any],
) -> dict[str, Any]:
    target = provider_root / _safe_component(game.slug)
    _write_json(target / "result.json", result.to_dict())
    _write_json(target / "purchase-coverage.json", coverage)
    return {
        "game": _game_dict(game),
        "runtime_status": result.status,
        "runtime_error": result.error,
        "run_dir": result.run_dir,
        "coverage": coverage,
    }


def _execute_purchase_game(
    provider,
    game: Game,
    *,
    timeout_s: float,
    stop_event: threading.Event,
    progress,
) -> tuple[GameTestResult, dict[str, Any], bool]:
    prefix = f"[{provider.key}/{game.slug}]"

    def game_progress(message: str) -> None:
        progress(f"{prefix} {message}")

    runtime_exc: BaseException | None = None
    try:
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

    runtime_failed = runtime_exc is not None or str(result.status or "").upper() in {
        "ERROR",
        "CANCELADO",
        "CANCELLED",
    }
    if runtime_failed:
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
            game_progress(f"clasificación de compras ERROR: {type(exc).__name__}: {exc}")

    state = str(coverage.get("state") or PURCHASE_UNKNOWN)
    counts = coverage.get("counts") if isinstance(coverage.get("counts"), dict) else {}
    game_progress(
        "PURCHASE "
        f"state={state} total={counts.get('total', 0)} "
        f"complete={counts.get('complete', 0)} "
        f"failed={counts.get('failed', 0)} unknown={counts.get('unknown', 0)}"
    )
    return result, coverage, runtime_failed


def run_selected_games(
    provider,
    games: list[Game],
    *,
    timeout_s: float,
    stop_event: threading.Event,
    output_dir: Path,
    progress,
    inter_game_delay_s: float = 0.0,
    circuit_breaker_threshold: int = 0,
    concurrency: int = 1,
    min_requests_per_minute: int = 250,
    rate_backoff_factor: float = 0.5,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    provider_root = Path(output_dir) / _safe_component(provider.key)
    previous_failure_fingerprint = ""
    consecutive_equivalent_failures = 0
    delay_s = max(0.0, float(inter_game_delay_s))
    breaker_threshold = max(0, int(circuit_breaker_threshold))
    requested_concurrency = max(1, int(concurrency))
    effective_concurrency = provider.effective_test_concurrency(requested_concurrency)
    min_rpm = max(1, int(min_requests_per_minute))
    factor = float(rate_backoff_factor)
    if not 0.0 < factor < 1.0:
        raise ValueError("rate_backoff_factor must be between 0 and 1")

    rate_snapshot = provider.provider_request_rate_snapshot()
    current_rpm = int(rate_snapshot.get("requests_per_minute_limit") or min_rpm)
    current_rpm = max(min_rpm, current_rpm)

    if effective_concurrency != requested_concurrency:
        progress(
            f"[{provider.key}] concurrency requested={requested_concurrency} "
            f"provider_cap={effective_concurrency}"
        )

    queue = list(games)
    in_flight: dict[Future, tuple[int, Game]] = {}
    next_index = 0
    last_start = 0.0
    breaker_open = False
    breaker_reason = ""

    def backoff_rate_if_needed(error: str) -> None:
        nonlocal current_rpm
        if not _is_transport_blocking_failure(error):
            return
        if current_rpm <= min_rpm:
            return
        next_rpm = max(min_rpm, int(current_rpm * factor))
        if next_rpm >= current_rpm:
            next_rpm = max(min_rpm, current_rpm - 1)
        if next_rpm >= current_rpm:
            return
        current_rpm = int(provider.set_provider_request_rate_limit(next_rpm))
        progress(
            f"[{provider.key}] transport pressure detected; "
            f"shared rate reduced to {current_rpm} rpm"
        )

    def update_breaker(result: GameTestResult, runtime_failed: bool) -> None:
        nonlocal previous_failure_fingerprint
        nonlocal consecutive_equivalent_failures
        nonlocal breaker_open
        nonlocal breaker_reason

        transport_failure = _is_transport_blocking_failure(result.error)
        if transport_failure:
            backoff_rate_if_needed(result.error)
        breaker_failure = runtime_failed or transport_failure
        if breaker_failure:
            fingerprint = _runtime_failure_fingerprint(result.error)
            if fingerprint == previous_failure_fingerprint:
                consecutive_equivalent_failures += 1
            else:
                previous_failure_fingerprint = fingerprint
                consecutive_equivalent_failures = 1
        else:
            previous_failure_fingerprint = ""
            consecutive_equivalent_failures = 0

        if (
            breaker_threshold > 0
            and breaker_failure
            and consecutive_equivalent_failures >= breaker_threshold
        ):
            breaker_open = True
            breaker_reason = (
                "Provider circuit breaker opened after "
                f"{consecutive_equivalent_failures} consecutive equivalent runtime failures: "
                f"{result.error}"
            )
            progress(
                f"[{provider.key}] {breaker_reason}; no se programarán más juegos en este lote."
            )

    with ThreadPoolExecutor(
        max_workers=effective_concurrency,
        thread_name_prefix=f"purchase-{provider.key}",
    ) as pool:
        while (next_index < len(queue) or in_flight) and not stop_event.is_set():
            while (
                not breaker_open
                and next_index < len(queue)
                and len(in_flight) < effective_concurrency
                and not stop_event.is_set()
            ):
                if last_start and delay_s > 0:
                    remaining = delay_s - (time.monotonic() - last_start)
                    if remaining > 0:
                        progress(f"[{provider.key}] cooldown entre aperturas: {remaining:g}s")
                        if stop_event.wait(remaining):
                            break

                game = queue[next_index]
                ordinal = next_index
                next_index += 1
                progress(
                    f"[{provider.key}] iniciando {next_index}/{len(queue)} {game.slug} "
                    f"(activos={len(in_flight) + 1}/{effective_concurrency})"
                )
                future = pool.submit(
                    _execute_purchase_game,
                    provider,
                    game,
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=progress,
                )
                in_flight[future] = (ordinal, game)
                last_start = time.monotonic()

            if not in_flight:
                break

            done, _pending = wait(
                tuple(in_flight),
                timeout=0.25,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                continue

            for future in done:
                _ordinal, game = in_flight.pop(future)
                try:
                    result, coverage, runtime_failed = future.result()
                except BaseException as exc:
                    result = _error_result(game, exc)
                    coverage = _unknown_coverage(result, result.error)
                    runtime_failed = True
                rows.append(_persist_row(provider_root, game, result, coverage))
                update_breaker(result, runtime_failed)

        while in_flight:
            done, _pending = wait(
                tuple(in_flight),
                timeout=0.25,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                _ordinal, game = in_flight.pop(future)
                try:
                    result, coverage, runtime_failed = future.result()
                except BaseException as exc:
                    result = _error_result(game, exc)
                    coverage = _unknown_coverage(result, result.error)
                    runtime_failed = True
                rows.append(_persist_row(provider_root, game, result, coverage))
                if not breaker_open:
                    update_breaker(result, runtime_failed)

    if breaker_open and next_index < len(queue):
        for skipped_game in queue[next_index:]:
            skipped_result = _error_result(skipped_game, RuntimeError(breaker_reason))
            skipped_coverage = _unknown_coverage(skipped_result, breaker_reason)
            rows.append(
                _persist_row(provider_root, skipped_game, skipped_result, skipped_coverage)
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
    parser.add_argument(
        "--requests-per-minute",
        type=int,
        default=30,
        help="Techo inicial de requests de protocolo por proveedor/minuto.",
    )
    parser.add_argument(
        "--min-requests-per-minute",
        type=int,
        default=250,
        help="Piso del descenso adaptativo ante presión del servidor.",
    )
    parser.add_argument(
        "--rate-backoff-factor",
        type=float,
        default=0.5,
        help="Factor multiplicativo aplicado al techo ante 403/429/503/timeout.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Juegos simultáneos solicitados; cada proveedor aplica su propio cap seguro.",
    )
    parser.add_argument(
        "--inter-game-delay",
        type=float,
        default=4.0,
        help="Pausa autoimpuesta entre aperturas de juegos.",
    )
    parser.add_argument(
        "--circuit-breaker-threshold",
        type=int,
        default=3,
        help="Corta nuevas aperturas tras N fallos runtime equivalentes consecutivos; 0 desactiva.",
    )
    parser.add_argument("--data-root", default="purchase-results/data")
    parser.add_argument("--output-dir", default="purchase-results")
    return parser


def _select_provider_games(
    provider,
    args: argparse.Namespace,
    stop_event,
    progress,
) -> tuple[list[Game], dict[str, Any]]:
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
    log_lock = threading.Lock()

    def progress(message: str) -> None:
        clean = str(message or "").rstrip()
        if clean:
            print(clean, flush=True)
            with log_lock:
                log_lines.append(clean)

    all_rows: list[dict[str, Any]] = []
    provider_summaries: list[dict[str, Any]] = []

    for key in providers:
        provider = provider_class_for(key)(data_root)
        limiter_initial = attach_safety_limiter(
            provider,
            requests_per_minute=max(1, int(args.requests_per_minute)),
        )
        effective_concurrency = provider.effective_test_concurrency(
            max(1, int(args.concurrency))
        )
        progress(
            f"[{key}] safety rpm={limiter_initial.get('requests_per_minute_limit')} "
            f"floor={max(1, int(args.min_requests_per_minute))} "
            f"concurrency={effective_concurrency} "
            f"inter_game_delay={max(0.0, float(args.inter_game_delay)):g}s "
            f"circuit_breaker={max(0, int(args.circuit_breaker_threshold))}"
        )
        try:
            selected, catalog = _select_provider_games(provider, args, stop_event, progress)
        except BaseException as exc:
            provider_summaries.append(
                {
                    "provider": key,
                    "selected_count": 0,
                    "rate_limit": provider.provider_request_rate_snapshot(),
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
            inter_game_delay_s=max(0.0, float(args.inter_game_delay)),
            circuit_breaker_threshold=max(0, int(args.circuit_breaker_threshold)),
            concurrency=max(1, int(args.concurrency)),
            min_requests_per_minute=max(1, int(args.min_requests_per_minute)),
            rate_backoff_factor=float(args.rate_backoff_factor),
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
                "effective_concurrency": effective_concurrency,
                "rate_limit": provider.provider_request_rate_snapshot(),
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
    selected_count = sum(
        int(summary.get("selected_count") or 0) for summary in provider_summaries
    )
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
            "requests_per_minute": int(args.requests_per_minute),
            "min_requests_per_minute": int(args.min_requests_per_minute),
            "rate_backoff_factor": float(args.rate_backoff_factor),
            "concurrency": int(args.concurrency),
            "inter_game_delay": float(args.inter_game_delay),
            "circuit_breaker_threshold": int(args.circuit_breaker_threshold),
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
