from __future__ import annotations

import json
import threading
from pathlib import Path

from tester_spin.models import Game
from tester_spin.providers.base import Progress
from tester_spin.providers.bgaming.adapter import BGamingProvider as _BGamingProvider
from tester_spin.providers.bgaming.har_capture import (
    append_har_debug,
    ensure_analysis_har,
    find_existing_har,
)
from tester_spin.providers.bgaming.runtime import (
    is_demo_url,
    resolve_fresh_demo_url,
    sanitize_error_text,
)


class BGamingProvider(_BGamingProvider):
    """BGaming provider with suite-level reusable diagnostic artifact capture."""

    def har_artifact_dir(self, game: Game) -> Path | None:
        game_dir = self.game_dir(game)
        existing = find_existing_har(game_dir)
        if existing is not None:
            return existing.parent
        analysis_dir = game_dir / "analysis"
        if analysis_dir.is_dir():
            return analysis_dir
        return None

    def prepare_test_artifacts(
        self,
        game: Game,
        *,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> None:
        game_dir = self.game_dir(game)

        # Zero-network fast path: a manually supplied or previously captured HAR
        # is authoritative for reuse and must never be overwritten.
        existing = find_existing_har(game_dir)
        if existing is not None:
            try:
                relative = existing.relative_to(game_dir)
            except ValueError:
                relative = existing
            append_har_debug(
                game_dir,
                "prepare_reuse_existing_har",
                path=str(relative),
                bytes=(existing.stat().st_size if existing.is_file() else 0),
            )
            progress(f"[{game.name}] HAR existente: {relative}; captura omitida.")
            return

        if stop_event.is_set():
            append_har_debug(game_dir, "prepare_cancelled_before_resolution")
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
            append_har_debug(
                game_dir,
                "prepare_complete",
                captured=result.captured,
                skipped=result.skipped,
                provider_posts=result.post_requests,
                error=result.error,
                path=str(result.path or ""),
            )
        finally:
            session.close()
            append_har_debug(game_dir, "prepare_session_closed")


__all__ = ["BGamingProvider"]
