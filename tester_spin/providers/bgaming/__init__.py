from __future__ import annotations

import json
import threading

from tester_spin.models import Game
from tester_spin.providers.base import Progress
from tester_spin.providers.bgaming.adapter import BGamingProvider as _BGamingProvider
from tester_spin.providers.bgaming.har_capture import ensure_analysis_har, find_existing_har
from tester_spin.providers.bgaming.runtime import (
    is_demo_url,
    resolve_fresh_demo_url,
    sanitize_error_text,
)


class BGamingProvider(_BGamingProvider):
    """BGaming provider with suite-level reusable diagnostic artifact capture."""

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
            progress(f"[{game.name}] HAR existente: {relative}; captura omitida.")
            return

        if stop_event.is_set():
            return

        public_url = ""
        game_json = game_dir / "game.json"
        if game_json.is_file():
            try:
                payload = json.loads(game_json.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    public_url = str(payload.get("public_url") or "").strip()
            except Exception:
                public_url = ""

        session = self._new_session()
        try:
            capture_url = ""
            source_url = public_url or game.url
            if source_url and not is_demo_url(source_url):
                try:
                    capture_url = resolve_fresh_demo_url(
                        session,
                        source_url,
                        timeout_s=timeout_s,
                    )
                except Exception as exc:
                    progress(
                        f"[{game.name}] HAR: no se pudo resolver demo fresco: "
                        f"{sanitize_error_text(f'{type(exc).__name__}: {exc}')}"
                    )
            elif is_demo_url(source_url):
                capture_url = source_url

            # A cataloged demo URL is still useful as fallback when public-page
            # resolution is temporarily unavailable.
            if not capture_url and is_demo_url(game.url):
                capture_url = game.url

            if not capture_url:
                progress(
                    f"[{game.name}] HAR: sin launch demo resoluble; captura omitida."
                )
                return

            ensure_analysis_har(
                game_dir=game_dir,
                game_name=game.name,
                launch_url=capture_url,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
            )
        finally:
            session.close()


__all__ = ["BGamingProvider"]
