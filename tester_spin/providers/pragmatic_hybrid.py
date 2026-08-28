from __future__ import annotations

import threading
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import GameCallback, Progress
from tester_spin.providers.pragmatic_catalog_ajax import crawl_pragmatic_catalog_ajax
from tester_spin.providers.pragmatic_endpoint import PragmaticProvider as _EndpointPragmaticProvider
from tester_spin.providers.pragmatic_protocol import analyze_response, summarize_analysis_files


class PragmaticProvider(_EndpointPragmaticProvider):
    """Pragmatic adapter with direct-AJAX catalog enumeration and endpoint game I/O.

    A complete browser HAR and Pragmatic's own ``gamesFilters`` JavaScript show that
    ``Load More Games`` increments a page counter and performs a same-origin GET to
    ``/en/games/?ajax=1...&page=N``. Catalog discovery therefore uses that endpoint
    directly, in ordered parallel batches, and falls back to the DOM implementation
    if the site changes or rejects the AJAX request.

    Once a game is selected, discovery/bootstrap and all game state transitions are
    handled by the endpoint-first implementation through HTTP/gameService. Every
    gameService response is additionally classified and fingerprinted so unknown or
    understood-but-unhandled protocol states remain machine-readable.
    """

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
    ) -> list[Game]:
        progress("Catálogo híbrido v6 AJAX: endpoint real de Load More + fallback DOM.")
        return crawl_pragmatic_catalog_ajax(
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

            unhandled = summary.get("unhandled_signatures") or summary.get("unknown_signatures") or []
            explicit = summary.get("explicit_actions") or {}
            progress(
                "Protocolo observado: "
                f"respuestas={summary.get('responses_analyzed', 0)}, "
                f"firmas no automatizadas={len(unhandled)}, "
                f"acciones explícitas={explicit or '{}'}"
            )

            if unhandled:
                progress(
                    "Los estados pendientes de automatización quedaron clasificados en "
                    "protocol-observations.json y en los *.analysis.json."
                )

        return result
