from __future__ import annotations

import threading
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import GameCallback, Progress
from tester_spin.providers.pragmatic_catalog_ajax import crawl_pragmatic_catalog_ajax
from tester_spin.providers.pragmatic_har_protocol import analyze_response, summarize_analysis_files
from tester_spin.providers.pragmatic_symbol_resolver import PragmaticProvider as _EndpointPragmaticProvider


class PragmaticProvider(_EndpointPragmaticProvider):
    """Pragmatic adapter with AJAX catalog enumeration and HAR-grounded game I/O.

    Catalog discovery uses Pragmatic's real same-origin Load More AJAX endpoint.
    Game execution remains endpoint-first and includes HAR-proven continuation
    transitions plus bootstrap-validated provider-symbol resolution.
    """

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
    ) -> list[Game]:
        progress("Catálogo híbrido v8 AJAX+HAR: Load More real + estados y símbolos validados.")
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
            automated = summary.get("har_automated_states") or {}
            progress(
                "Protocolo observado: "
                f"respuestas={summary.get('responses_analyzed', 0)}, "
                f"firmas no automatizadas={len(unhandled)}, "
                f"HAR handlers={automated or '{}'}, "
                f"acciones explícitas={explicit or '{}'}"
            )

            if unhandled:
                progress(
                    "Los estados pendientes de automatización quedaron clasificados en "
                    "protocol-observations.json y en los *.analysis.json."
                )

        return result
