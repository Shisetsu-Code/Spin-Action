from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tester_spin.models import utc_now_iso
from tester_spin.providers.base import Progress
from tester_spin.providers.bgaming.runtime import sanitize_error_text, sanitize_session_url


@dataclass(slots=True)
class HARCaptureResult:
    path: Path | None
    captured: bool
    skipped: bool
    post_requests: int = 0
    error: str = ""


def find_existing_har(game_dir: Path) -> Path | None:
    """Return any reusable non-empty HAR already stored for this game.

    A user-supplied HAR has priority over automatic capture simply by existing.
    Partial files created by this module are deliberately excluded.
    """
    root = Path(game_dir)
    if not root.exists():
        return None

    candidates: list[Path] = []
    for path in root.rglob("*.har"):
        if not path.is_file():
            continue
        if path.name.casefold().endswith(".partial.har"):
            continue
        try:
            if path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        candidates.append(path)

    if not candidates:
        return None

    preferred = root / "analysis" / "browser.har"
    if preferred in candidates:
        return preferred
    return sorted(candidates, key=lambda item: str(item).casefold())[0]


def _click_standard_entry_controls(page: Any) -> None:
    """Best-effort provider-generic entry controls; never depend on a game title."""
    labels = re.compile(
        r"^(play|play demo|start|continue|ok|accept|enter|tap to play)$",
        re.IGNORECASE,
    )
    try:
        buttons = page.get_by_role("button", name=labels)
        count = min(buttons.count(), 4)
        for index in range(count):
            try:
                buttons.nth(index).click(timeout=750)
                page.wait_for_timeout(500)
            except Exception:
                continue
    except Exception:
        pass


def _is_bgaming_state_post(request: Any) -> bool:
    try:
        if str(request.method).upper() != "POST":
            return False
        parsed = urlparse(str(request.url or ""))
        host = (parsed.hostname or "").casefold()
        if not (
            host == "bgaming-network.com"
            or host.endswith(".bgaming-network.com")
        ):
            return False
        path = parsed.path.casefold()
        return path == "/api" or "/api/" in path
    except Exception:
        return False


def _attempt_provider_generic_spin(page: Any, baseline_posts: int, get_posts) -> None:
    """Try standard canvas/keyboard interaction without title-specific selectors."""
    for _attempt in range(2):
        if get_posts() > baseline_posts:
            return

        focused = False
        for frame in list(page.frames):
            try:
                canvas = frame.locator("canvas").first
                if canvas.count() and canvas.is_visible(timeout=500):
                    canvas.click(timeout=750, force=True)
                    frame.locator("body").press("Space", timeout=750)
                    focused = True
                    page.wait_for_timeout(1800)
                    if get_posts() > baseline_posts:
                        return
            except Exception:
                continue

        if focused:
            continue

        try:
            page.mouse.click(720, 450)
            page.wait_for_timeout(400)
            page.keyboard.press("Space")
            page.wait_for_timeout(1800)
        except Exception:
            return


def _capture_browser_har(
    *,
    launch_url: str,
    target: Path,
    timeout_s: float,
    stop_event: threading.Event,
) -> int:
    """Record a full embedded-content HAR and attempt one demo spin.

    The interaction is intentionally generic: launch, dismiss ordinary DOM entry
    buttons, focus a canvas in any frame and press Space. If a particular runtime
    does not expose a standard shortcut, the HAR still preserves bootstrap/init,
    scripts and other network responses needed for protocol diagnosis.
    """
    from playwright.sync_api import sync_playwright

    timeout_ms = int(max(8.0, min(float(timeout_s), 30.0)) * 1000)
    post_requests = 0

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_har_path=str(target),
            record_har_content="embed",
            record_har_mode="full",
            service_workers="block",
            ignore_https_errors=False,
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        def on_request(request: Any) -> None:
            nonlocal post_requests
            if _is_bgaming_state_post(request):
                post_requests += 1

        page.on("request", on_request)
        try:
            page.goto(
                launch_url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            page.wait_for_timeout(2500)
            if stop_event.is_set():
                return post_requests

            _click_standard_entry_controls(page)
            page.wait_for_timeout(1200)

            baseline_posts = post_requests
            _attempt_provider_generic_spin(
                page,
                baseline_posts,
                lambda: post_requests,
            )
        finally:
            # HAR is flushed on context.close().
            try:
                context.close()
            finally:
                browser.close()

    return post_requests


def ensure_analysis_har(
    *,
    game_dir: Path,
    game_name: str,
    launch_url: str,
    timeout_s: float,
    stop_event: threading.Event,
    progress: Progress,
) -> HARCaptureResult:
    """Ensure one reusable HAR exists for a BGaming game.

    Existing HARs are never overwritten. Capture failures are diagnostic only and
    must not block the protocol test itself.
    """
    existing = find_existing_har(game_dir)
    if existing is not None:
        try:
            relative = existing.relative_to(game_dir)
        except ValueError:
            relative = existing
        progress(f"[{game_name}] HAR existente: {relative}; captura omitida.")
        return HARCaptureResult(
            path=existing,
            captured=False,
            skipped=True,
        )

    if stop_event.is_set():
        return HARCaptureResult(
            path=None,
            captured=False,
            skipped=True,
            error="captura cancelada",
        )

    analysis_dir = Path(game_dir) / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    target = analysis_dir / "browser.har"
    partial = analysis_dir / "browser.partial.har"
    metadata = analysis_dir / "har-capture.json"

    progress(f"[{game_name}] HAR ausente: capturando demo en Chromium headless...")
    started = time.monotonic()
    try:
        post_requests = _capture_browser_har(
            launch_url=launch_url,
            target=partial,
            timeout_s=timeout_s,
            stop_event=stop_event,
        )
        if not partial.is_file() or partial.stat().st_size <= 0:
            raise ValueError("Playwright no produjo un HAR no vacío")
        partial.replace(target)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        metadata.write_text(
            json.dumps(
                {
                    "schema": "tester-spin/bgaming-har-capture/v1",
                    "captured_at": utc_now_iso(),
                    "launch_url": sanitize_session_url(launch_url),
                    "path": str(target.name),
                    "bytes": target.stat().st_size,
                    "provider_post_requests": post_requests,
                    "elapsed_ms": elapsed_ms,
                    "interaction": "provider-generic-entry+canvas-space",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        progress(
            f"[{game_name}] HAR guardado: analysis/{target.name} "
            f"({target.stat().st_size / 1024 / 1024:.1f} MiB, "
            f"POSTs BGaming={post_requests})."
        )
        return HARCaptureResult(
            path=target,
            captured=True,
            skipped=False,
            post_requests=post_requests,
        )
    except Exception as exc:
        try:
            if partial.exists():
                partial.unlink()
        except OSError:
            pass
        message = sanitize_error_text(f"{type(exc).__name__}: {exc}")
        metadata.write_text(
            json.dumps(
                {
                    "schema": "tester-spin/bgaming-har-capture/v1",
                    "captured_at": utc_now_iso(),
                    "launch_url": sanitize_session_url(launch_url),
                    "error": message,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        progress(
            f"[{game_name}] HAR captura ERROR (la prueba continúa): {message}"
        )
        return HARCaptureResult(
            path=None,
            captured=False,
            skipped=False,
            error=message,
        )
