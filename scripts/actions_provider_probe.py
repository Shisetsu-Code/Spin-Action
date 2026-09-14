from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path
from typing import Any

from tester_spin.action_audit import build_action_audit
from tester_spin.farm_contract import export_farm_contract
from tester_spin.models import Game, GameTestResult
from tester_spin.providers import (
    BGamingProvider,
    BelatraProvider,
    OneSpin4WinProvider,
    PragmaticProvider,
    RedTigerProvider,
    RubyPlayProvider,
)


_PROVIDER_CLASSES = {
    "pragmatic": PragmaticProvider,
    "bgaming": BGamingProvider,
    "rubyplay": RubyPlayProvider,
    "redtiger": RedTigerProvider,
    "belatra": BelatraProvider,
    "1spin4win": OneSpin4WinProvider,
    "one_spin4win": OneSpin4WinProvider,
}

_VERDICT_PRIORITY = {
    "COMPLETE": 0,
    "UNKNOWN": 1,
    "INCOMPLETE": 2,
    "CANCELLED": 3,
    "ERROR": 4,
}


def provider_class_for(key: str):
    normalized = str(key or "").strip().lower()
    try:
        return _PROVIDER_CLASSES[normalized]
    except KeyError as exc:
        raise ValueError(f"Proveedor no soportado: {key!r}") from exc


def select_games(
    games: list[Game],
    *,
    slug: str,
    offset: int,
    limit: int,
) -> list[Game]:
    exact_slug = str(slug or "").strip()
    ordered = sorted(
        games,
        key=lambda game: (game.slug.casefold(), game.name.casefold(), game.url),
    )
    if exact_slug:
        return [game for game in ordered if game.slug == exact_slug]

    start = max(0, int(offset))
    remaining = ordered[start:]
    count = int(limit)
    if count <= 0:
        return remaining
    return remaining[:count]


def summarize_audits(audits: list[dict[str, Any]]) -> str:
    if not audits:
        return "UNKNOWN"
    verdicts = [str(audit.get("verdict") or "UNKNOWN").upper() for audit in audits]
    return max(verdicts, key=lambda value: _VERDICT_PRIORITY.get(value, 4))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Ejecuta un proveedor de Tester-Spin en modo laboratorio y aplica un "
            "gate fail-closed independiente del status runtime."
        )
    )
    parser.add_argument("--provider", required=True, choices=sorted(_PROVIDER_CLASSES))
    parser.add_argument("--slug", default="", help="Slug exacto; vacío usa offset/limit.")
    parser.add_argument("--game-offset", type=int, default=0)
    parser.add_argument("--game-limit", type=int, default=1)
    parser.add_argument("--spins", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="Límite conservador del crawl. 0 significa catálogo completo.",
    )
    parser.add_argument("--data-root", default="action-results/data")
    parser.add_argument("--output-dir", default="action-results")
    parser.add_argument(
        "--allow-har-fallback",
        action="store_true",
        help=(
            "Permite prepare_test_artifacts(). Desactivado por defecto para evitar "
            "capturas/descargas HAR innecesarias."
        ),
    )
    return parser


def _game_dict(game: Game) -> dict[str, Any]:
    return {
        "provider": game.provider,
        "slug": game.slug,
        "name": game.name,
        "url": game.url,
        "symbol": game.symbol,
        "thumbnail_url": game.thumbnail_url,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _error_result(game: Game, spins: int, exc: BaseException) -> GameTestResult:
    return GameTestResult(
        provider=game.provider,
        slug=game.slug,
        game_name=game.name,
        game_url=game.url,
        requested_spins=max(1, int(spins)),
        successful_spins=0,
        failed_spins=max(1, int(spins)),
        status="ERROR",
        symbol=game.symbol,
        error=f"{type(exc).__name__}: {exc}",
    )


def run_probe(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    output_dir = Path(args.output_dir).resolve()
    data_root = Path(args.data_root).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)

    provider_cls = provider_class_for(args.provider)
    provider = provider_cls(data_root)
    stop_event = threading.Event()
    log_lines: list[str] = []

    def progress(message: str) -> None:
        clean = str(message or "").rstrip()
        if clean:
            print(clean, flush=True)
            log_lines.append(clean)

    requested_pages = int(args.max_pages)
    if requested_pages < 0:
        raise ValueError("--max-pages debe ser 0 o positivo")
    crawl_pages = 10_000 if requested_pages == 0 else max(1, requested_pages)

    progress(
        f"LAB provider={provider.key} pages={requested_pages} spins={max(1, int(args.spins))} "
        f"har_fallback={'enabled' if args.allow_har_fallback else 'disabled'}"
    )
    provider.set_catalog_authority(True, "")
    games = provider.crawl_catalog(
        stop_event=stop_event,
        progress=progress,
        max_pages=crawl_pages,
        on_game=None,
    )
    _write_json(
        output_dir / "catalog.json",
        {
            "provider": provider.key,
            "count": len(games),
            "authoritative": bool(provider.catalog_crawl_authoritative),
            "authority_reason": str(provider.catalog_crawl_reason or ""),
            "games": [_game_dict(game) for game in games],
        },
    )

    selected = select_games(
        games,
        slug=args.slug,
        offset=args.game_offset,
        limit=args.game_limit,
    )
    progress(
        "Selección: "
        + (", ".join(game.slug for game in selected) if selected else "<vacía>")
    )

    audits: list[dict[str, Any]] = []
    result_rows: list[dict[str, Any]] = []
    spins = max(1, int(args.spins))
    timeout_s = max(1.0, float(args.timeout))

    for game in selected:
        game_progress = lambda message, name=game.name: progress(f"[{name}] {message}")
        try:
            if args.allow_har_fallback:
                game_progress("HAR fallback habilitado explícitamente: preparando artefactos.")
                provider.prepare_test_artifacts(
                    game,
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=game_progress,
                )
            else:
                game_progress(
                    "HAR fallback deshabilitado: prepare_test_artifacts() omitido; "
                    "se usa sólo wire/bootstrap normal."
                )

            result = provider.test_game(
                game,
                spins=spins,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=game_progress,
            )
            result.samples_per_path = spins
            result = provider.finalize_test_result(result, progress=game_progress)
            export_farm_contract(
                provider,
                game,
                result,
                progress=game_progress,
            )
        except BaseException as exc:
            result = _error_result(game, spins, exc)
            game_progress(f"ERROR de laboratorio: {result.error}")

        audit = build_action_audit(result)
        audits.append(audit)
        result_rows.append(
            {
                "game": _game_dict(game),
                "runtime_status": result.status,
                "runtime_error": result.error,
                "run_dir": result.run_dir,
                "audit_verdict": audit["verdict"],
            }
        )
        _write_json(output_dir / provider.key / game.slug / "audit.json", audit)
        _write_json(output_dir / provider.key / game.slug / "result.json", result.to_dict())
        game_progress(
            f"VEREDICTO LAB={audit['verdict']} runtime={result.status} "
            f"acciones={audit['counts']['actions']} "
            f"demostradas={audit['counts']['demonstrated']} "
            f"incompletas={audit['counts']['incomplete']} "
            f"desconocidas={audit['counts']['unknown']}"
        )

    overall = summarize_audits(audits)
    if not selected:
        overall = "UNKNOWN"

    summary = {
        "schema": "tester-spin/actions-provider-probe/v1",
        "provider": provider.key,
        "overall_verdict": overall,
        "settings": {
            "slug": str(args.slug or ""),
            "game_offset": int(args.game_offset),
            "game_limit": int(args.game_limit),
            "spins": spins,
            "timeout": timeout_s,
            "max_pages": requested_pages,
            "allow_har_fallback": bool(args.allow_har_fallback),
        },
        "catalog_count": len(games),
        "selected_count": len(selected),
        "results": result_rows,
        "audits": audits,
    }
    if not selected:
        summary["reason"] = "No se seleccionó ningún juego; no hay evidencia suficiente."

    _write_json(output_dir / "summary.json", summary)
    (output_dir / "probe.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    progress(f"VEREDICTO GENERAL={overall}")
    return overall, summary


def main() -> int:
    args = build_parser().parse_args()
    try:
        verdict, _summary = run_probe(args)
    except BaseException as exc:
        output_dir = Path(getattr(args, "output_dir", "action-results")).resolve()
        _write_json(
            output_dir / "summary.json",
            {
                "schema": "tester-spin/actions-provider-probe/v1",
                "provider": str(getattr(args, "provider", "")),
                "overall_verdict": "ERROR",
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )
        print(f"PROBE ERROR: {type(exc).__name__}: {exc}", flush=True)
        return 2
    return 0 if verdict == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
