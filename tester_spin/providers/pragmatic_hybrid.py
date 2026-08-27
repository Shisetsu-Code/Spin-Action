from __future__ import annotations

import threading
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import GameCallback, Progress
from tester_spin.providers.pragmatic_catalog_preloaded import crawl_pragmatic_catalog_preloaded
from tester_spin.providers.pragmatic_endpoint import PragmaticProvider as _EndpointPragmaticProvider
from tester_spin.providers.pragmatic_protocol import analyze_response, summarize_analysis_files


class PragmaticProvider(_EndpointPragmaticProvider):
    """Pragmatic adapter with preloaded-DOM catalog enumeration and endpoint game I/O.

    The observed catalog does not need a catalog XHR for Load More: cards can already
    exist client-side and only become visible/lazy-load images after each click.
    Catalog discovery therefore enumerates hidden/preloaded cards first and uses Load
    More only to detect genuine structural additions.

    Once a game is selected, discovery/bootstrap and all game state transitions are
    handled by the endpoint-first implementation through HTTP/gameService. Every
    gameService response is additionally classified and fingerprinted so unknown
    protocol states remain machine-readable instead of only producing a warning.
    """

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
    ) -> list[Game]:
        progress("Catálogo híbrido v5 ULTRA: DOM oculto/preloaded + verificación Load More.")
        return crawl_pragmatic_catalog_preloaded(
            self,
            stop_event=stop_event,
            progress=progress,
            max_pages=max_pages,
            on_game=on_game,
        )

    def _post_and_store(self, bootstrap, fields, root: Path, *, step: int, label: str, timeout_s: float):
        result = super()._post_and_store(
            bootstrap,
            fields,
            root,
            step=step,
            label=label,
            timeout_s=timeout_s,
        )
        parsed = result[2]
        analysis = analyze_response(parsed)
        self._write_json(root / f"step-{step:03d}-{label}.analysis.json", analysis)
        return result

    def _write_discovery(self, run_root, discovery, catalog) -> None:
        super()._write_discovery(run_root, discovery, catalog)
        root = Path(run_root) / "discovery"
        self._write_json(root / "doInit.response.analysis.json", analyze_response(discovery.init_response))
        self._write_json(
            root / "calibration.response.analysis.json",
            analyze_response(discovery.calibration_response),
        )

    def _write_http_bootstrap(self, root, bootstrap) -> None:
        super()._write_http_bootstrap(root, bootstrap)
        boot = Path(root) / "bootstrap"
        self._write_json(boot / "doInit.response.analysis.json", analyze_response(bootstrap.init_response))
        self._write_json(
            boot / "calibration.response.analysis.json",
            analyze_response(bootstrap.calibration_response),
        )

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        result = super().test_game(
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )

        if result.run_dir:
            run_root = Path(result.run_dir)
            summary = summarize_analysis_files(run_root)
            self._write_json(run_root / "protocol-observations.json", summary)

            unknown = summary.get("unknown_signatures") or []
            explicit = summary.get("explicit_actions") or {}
            progress(
                "Protocolo observado: "
                f"respuestas={summary.get('responses_analyzed', 0)}, "
                f"firmas desconocidas={len(unknown)}, "
                f"acciones explícitas={explicit or '{}'}"
            )

            if unknown:
                progress(
                    "Los estados aún no ejecutables quedaron clasificados en "
                    "protocol-observations.json y en los *.analysis.json."
                )

        return result
