from __future__ import annotations

import json
import threading
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.bgaming.adapter import BGamingProvider as _BGamingProvider
from tester_spin.providers.bgaming.har_capture import (
    append_har_debug,
    ensure_analysis_har,
)
from tester_spin.providers.bgaming.har_select import inspect_har, select_best_har
from tester_spin.providers.bgaming.hyperhive_wire import install_observed_wire_adapter
from tester_spin.providers.bgaming.runner_diagnostics import diagnose_progress_event
from tester_spin.providers.bgaming.runtime import (
    is_demo_url,
    resolve_fresh_demo_url,
    sanitize_error_text,
)


# HyperHive clients do not all serialize the same play payload. Install the
# provider-local adapter once so execution follows the contract demonstrated by
# the scripts/HAR loaded by each runtime instead of a game-name allowlist.
install_observed_wire_adapter()


class BGamingProvider(_BGamingProvider):
    """BGaming provider with suite-level reusable diagnostic artifact capture."""

    def har_artifact_dir(self, game: Game) -> Path | None:
        game_dir = self.game_dir(game)
        existing = select_best_har(game_dir)
        if existing is not None:
            return existing.parent
        analysis_dir = game_dir / "analysis"
        if analysis_dir.is_dir():
            return analysis_dir
        return None

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        """Run BGaming while persisting the same diagnostic stream shown in the GUI."""
        game_dir = self.game_dir(game)
        append_har_debug(
            game_dir,
            "runner_start",
            requested_spins=spins,
            timeout_s=timeout_s,
            game_url=game.url,
            symbol=game.symbol,
        )

        def logged_progress(message: str) -> None:
            append_har_debug(
                game_dir,
                "runner_progress",
                message=str(message),
            )
            progress(message)

            diagnostic = diagnose_progress_event(game_dir, message)
            if diagnostic is None:
                return
            diagnostic_message = str(diagnostic.get("message") or "").strip()
            append_har_debug(
                game_dir,
                "runner_error_context",
                **{
                    key: value
                    for key, value in diagnostic.items()
                    if key != "message"
                },
            )
            if diagnostic_message:
                progress(f"[{game.name}] DIAGNÓSTICO: {diagnostic_message}")

        try:
            result = super().test_game(
                game,
                spins=spins,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=logged_progress,
            )
        except Exception as exc:
            append_har_debug(
                game_dir,
                "runner_exception",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        append_har_debug(
            game_dir,
            "runner_result",
            status=result.status,
            requested_spins=result.requested_spins,
            successful_spins=result.successful_spins,
            failed_spins=result.failed_spins,
            elapsed_ms=result.elapsed_ms,
            error=result.error,
            run_dir=result.run_dir,
            discovered_modes=[
                {
                    "id": mode.get("id"),
                    "kind": mode.get("kind"),
                    "executable": mode.get("executable"),
                    "discovery_state": mode.get("discovery_state"),
                }
                for mode in result.discovered_modes[:50]
                if isinstance(mode, dict)
            ],
        )
        return result

    def prepare_test_artifacts(
        self,
        game: Game,
        *,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> None:
        game_dir = self.game_dir(game)
        automatic_har = game_dir / "analysis" / "browser.har"
        incomplete_backup = game_dir / "analysis" / "browser.bootstrap-only.bak"
        moved_incomplete_automatic = False

        # Reuse only protocol-useful HARs without question. A manually supplied
        # HAR is never overwritten even when it contains bootstrap only. The one
        # exception is our own analysis/browser.har: if that automatic artifact
        # has no play/spin evidence it must not permanently suppress future
        # capture attempts.
        existing = select_best_har(game_dir)
        if existing is not None:
            quality = inspect_har(existing)
            try:
                relative = existing.relative_to(game_dir)
            except ValueError:
                relative = existing

            is_automatic = False
            try:
                is_automatic = existing.resolve() == automatic_har.resolve()
            except OSError:
                is_automatic = existing == automatic_har

            if quality is not None and quality.protocol_usable:
                append_har_debug(
                    game_dir,
                    "prepare_reuse_existing_har",
                    path=str(relative),
                    bytes=(existing.stat().st_size if existing.is_file() else 0),
                    quality=quality.grade,
                    operations=quality.operations,
                    plays=quality.plays,
                    spins=quality.spins,
                    purchases=quality.purchases,
                )
                progress(
                    f"[{game.name}] HAR seleccionado: {relative}; "
                    f"calidad={quality.grade}, operaciones={quality.operations}, "
                    f"compras={quality.purchases}; captura omitida."
                )
                return

            if not is_automatic:
                append_har_debug(
                    game_dir,
                    "prepare_reuse_manual_incomplete_har",
                    path=str(relative),
                    bytes=(existing.stat().st_size if existing.is_file() else 0),
                    quality=(quality.grade if quality is not None else "UNKNOWN"),
                )
                progress(
                    f"[{game.name}] HAR manual existente: {relative}; "
                    f"calidad={quality.grade if quality is not None else 'UNKNOWN'}; "
                    "no se sobrescribe."
                )
                return

            try:
                incomplete_backup.parent.mkdir(parents=True, exist_ok=True)
                if incomplete_backup.exists():
                    incomplete_backup.unlink()
                existing.replace(incomplete_backup)
                moved_incomplete_automatic = True
                append_har_debug(
                    game_dir,
                    "prepare_retry_incomplete_automatic_har",
                    previous_quality=(quality.grade if quality is not None else "UNKNOWN"),
                    backup=incomplete_backup.name,
                )
                progress(
                    f"[{game.name}] HAR automático incompleto "
                    f"({quality.grade if quality is not None else 'UNKNOWN'}): "
                    "sin play/spin; reintentando captura."
                )
            except OSError as exc:
                append_har_debug(
                    game_dir,
                    "prepare_incomplete_har_backup_failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                progress(
                    f"[{game.name}] HAR automático incompleto pero no pudo "
                    "apartarse para recaptura; se conserva."
                )
                return

        if stop_event.is_set():
            append_har_debug(game_dir, "prepare_cancelled_before_resolution")
            if moved_incomplete_automatic and incomplete_backup.exists():
                incomplete_backup.replace(automatic_har)
            return

        public_url = ""
        game_json = game_dir / "game.json"
        if game_json.is_file():
            try:
                payload = json.loads(game_json.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    public_url = str(payload.get("public_url") or "").strip()
            except Exception as exc:
                append_har_debug(
                    game_dir,
                    "prepare_game_metadata_read_failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                public_url = ""

        session = self._new_session()
        try:
            capture_url = ""
            source_url = public_url or game.url
            append_har_debug(
                game_dir,
                "prepare_start",
                source_url=source_url,
                source_kind="public_url" if public_url else "catalog_url",
                timeout_s=timeout_s,
            )

            if source_url and not is_demo_url(source_url):
                try:
                    progress(f"[{game.name}] HAR: resolviendo demo fresco...")
                    append_har_debug(
                        game_dir,
                        "demo_resolution_start",
                        source_url=source_url,
                    )
                    capture_url = resolve_fresh_demo_url(
                        session,
                        source_url,
                        timeout_s=timeout_s,
                    )
                    if capture_url:
                        append_har_debug(
                            game_dir,
                            "demo_resolution_ok",
                            launch_url=capture_url,
                        )
                        progress(f"[{game.name}] HAR: demo fresco resuelto.")
                    else:
                        append_har_debug(
                            game_dir,
                            "demo_resolution_empty",
                            source_url=source_url,
                        )
                except Exception as exc:
                    message = sanitize_error_text(
                        f"{type(exc).__name__}: {exc}"
                    )
                    append_har_debug(
                        game_dir,
                        "demo_resolution_failed",
                        source_url=source_url,
                        error=message,
                    )
                    progress(
                        f"[{game.name}] HAR: no se pudo resolver demo fresco: "
                        f"{message}"
                    )
            elif is_demo_url(source_url):
                capture_url = source_url
                append_har_debug(
                    game_dir,
                    "demo_resolution_not_needed",
                    launch_url=capture_url,
                )
                progress(f"[{game.name}] HAR: usando launch demo ya catalogado.")

            # A cataloged demo URL is still useful as fallback when public-page
            # resolution is temporarily unavailable.
            if not capture_url and is_demo_url(game.url):
                capture_url = game.url
                append_har_debug(
                    game_dir,
                    "demo_resolution_catalog_fallback",
                    launch_url=capture_url,
                )
                progress(f"[{game.name}] HAR: usando launch demo fallback del catálogo.")

            if not capture_url:
                append_har_debug(
                    game_dir,
                    "prepare_no_demo_url",
                    source_url=source_url,
                )
                progress(
                    f"[{game.name}] HAR: sin launch demo resoluble; captura omitida."
                )
                return

            result = ensure_analysis_har(
                game_dir=game_dir,
                game_name=game.name,
                launch_url=capture_url,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
            )
            result_quality = inspect_har(result.path)
            append_har_debug(
                game_dir,
                "prepare_complete",
                captured=result.captured,
                skipped=result.skipped,
                provider_posts=result.post_requests,
                error=result.error,
                path=str(result.path or ""),
                quality=(result_quality.grade if result_quality is not None else "UNKNOWN"),
                operations=(result_quality.operations if result_quality is not None else 0),
            )
            if result.path is not None and result_quality is not None:
                if result_quality.protocol_usable:
                    progress(
                        f"[{game.name}] HAR listo para protocolo: "
                        f"{result_quality.grade}, operaciones={result_quality.operations}."
                    )
                else:
                    progress(
                        f"[{game.name}] HAR sigue incompleto: {result_quality.grade}; "
                        "no contiene play/spin y se reintentará en la próxima corrida."
                    )
        finally:
            session.close()
            # Keep a failed recapture from destroying the previous bootstrap HAR.
            if moved_incomplete_automatic and incomplete_backup.exists():
                if automatic_har.is_file() and automatic_har.stat().st_size > 0:
                    try:
                        incomplete_backup.unlink()
                    except OSError:
                        pass
                else:
                    try:
                        incomplete_backup.replace(automatic_har)
                        append_har_debug(
                            game_dir,
                            "prepare_restored_previous_automatic_har",
                            path=str(automatic_har),
                        )
                    except OSError as exc:
                        append_har_debug(
                            game_dir,
                            "prepare_restore_previous_har_failed",
                            error=f"{type(exc).__name__}: {exc}",
                        )
            append_har_debug(game_dir, "prepare_session_closed")


__all__ = ["BGamingProvider"]
