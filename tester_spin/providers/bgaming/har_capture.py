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


def _debug_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return sanitize_session_url(value)
        return sanitize_error_text(value)[:2000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_debug_value(item) for item in list(value)[:40]]
    if isinstance(value, dict):
        return {
            str(key)[:120]: _debug_value(item)
            for key, item in list(value.items())[:40]
        }
    return sanitize_error_text(repr(value))[:2000]


def append_har_debug(game_dir: Path, stage: str, **fields: Any) -> Path:
    """Append one sanitized diagnostic event without ever failing the suite."""
    path = Path(game_dir) / "analysis" / "har-debug.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "at": utc_now_iso(),
            "stage": str(stage),
            **{str(key): _debug_value(value) for key, value in fields.items()},
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return path


def _click_standard_entry_controls(page: Any, game_dir: Path) -> int:
    """Best-effort provider-generic entry controls; never depend on a game title."""
    labels = re.compile(
        r"^(play|play demo|start|continue|ok|accept|enter|tap to play)$",
        re.IGNORECASE,
    )
    clicked = 0
    try:
        buttons = page.get_by_role("button", name=labels)
        count = min(buttons.count(), 4)
        append_har_debug(game_dir, "entry_controls_scan", matching_buttons=count)
        for index in range(count):
            try:
                label = ""
                try:
                    label = buttons.nth(index).inner_text(timeout=400)
                except Exception:
                    label = ""
                buttons.nth(index).click(timeout=750)
                clicked += 1
                append_har_debug(
                    game_dir,
                    "entry_control_clicked",
                    index=index,
                    label=label,
                )
                page.wait_for_timeout(500)
            except Exception as exc:
                append_har_debug(
                    game_dir,
                    "entry_control_click_failed",
                    index=index,
                    error=f"{type(exc).__name__}: {exc}",
                )
    except Exception as exc:
        append_har_debug(
            game_dir,
            "entry_controls_scan_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    return clicked


def _is_bgaming_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").casefold()
        return host == "bgaming-network.com" or host.endswith(".bgaming-network.com")
    except Exception:
        return False


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


def _frame_summary(page: Any) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for index, frame in enumerate(list(page.frames)[:20]):
        entry: dict[str, Any] = {
            "index": index,
            "url": sanitize_session_url(str(getattr(frame, "url", "") or "")),
        }
        try:
            entry["canvas_count"] = int(frame.locator("canvas").count())
        except Exception as exc:
            entry["canvas_error"] = sanitize_error_text(
                f"{type(exc).__name__}: {exc}"
            )[:400]
        summary.append(entry)
    return summary


def _attempt_provider_generic_spin(
    page: Any,
    baseline_posts: int,
    get_posts,
    game_dir: Path,
) -> None:
    """Try standard canvas/keyboard interaction without title-specific selectors."""
    append_har_debug(
        game_dir,
        "spin_probe_start",
        baseline_provider_posts=baseline_posts,
        frames=_frame_summary(page),
    )

    for attempt in range(1, 3):
        if get_posts() > baseline_posts:
            append_har_debug(
                game_dir,
                "spin_probe_already_observed",
                attempt=attempt,
                provider_posts=get_posts(),
            )
            return

        focused = False
        for frame_index, frame in enumerate(list(page.frames)):
            try:
                canvas = frame.locator("canvas").first
                if canvas.count() and canvas.is_visible(timeout=500):
                    append_har_debug(
                        game_dir,
                        "spin_probe_canvas",
                        attempt=attempt,
                        frame_index=frame_index,
                        frame_url=str(getattr(frame, "url", "") or ""),
                    )
                    canvas.click(timeout=750, force=True)
                    frame.locator("body").press("Space", timeout=750)
                    focused = True
                    page.wait_for_timeout(1800)
                    if get_posts() > baseline_posts:
                        append_har_debug(
                            game_dir,
                            "spin_probe_success",
                            method="frame-canvas-space",
                            attempt=attempt,
                            provider_posts=get_posts(),
                        )
                        return
            except Exception as exc:
                append_har_debug(
                    game_dir,
                    "spin_probe_canvas_failed",
                    attempt=attempt,
                    frame_index=frame_index,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue

        if focused:
            append_har_debug(
                game_dir,
                "spin_probe_no_post_after_canvas",
                attempt=attempt,
                provider_posts=get_posts(),
            )
            continue

        try:
            append_har_debug(
                game_dir,
                "spin_probe_fallback",
                attempt=attempt,
                method="page-center-space",
            )
            page.mouse.click(720, 450)
            page.wait_for_timeout(400)
            page.keyboard.press("Space")
            page.wait_for_timeout(1800)
            if get_posts() > baseline_posts:
                append_har_debug(
                    game_dir,
                    "spin_probe_success",
                    method="page-center-space",
                    attempt=attempt,
                    provider_posts=get_posts(),
                )
                return
        except Exception as exc:
            append_har_debug(
                game_dir,
                "spin_probe_fallback_failed",
                attempt=attempt,
                error=f"{type(exc).__name__}: {exc}",
            )
            return

    append_har_debug(
        game_dir,
        "spin_probe_exhausted",
        provider_posts=get_posts(),
        baseline_provider_posts=baseline_posts,
    )


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
    console_events = 0
    game_dir = target.parent.parent

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    debug_log = target.parent / "har-debug.jsonl"
    try:
        debug_log.write_text("", encoding="utf-8")
    except Exception:
        pass

    append_har_debug(
        game_dir,
        "capture_start",
        launch_url=launch_url,
        timeout_ms=timeout_ms,
        target=target.name,
    )

    try:
        append_har_debug(game_dir, "playwright_start")
        with sync_playwright() as playwright:
            append_har_debug(game_dir, "browser_launch_start", headless=True)
            browser = playwright.chromium.launch(
                headless=True,
                args=["--autoplay-policy=no-user-gesture-required"],
            )
            append_har_debug(game_dir, "browser_launch_ok")
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                record_har_path=str(target),
                record_har_content="embed",
                record_har_mode="full",
                service_workers="block",
                ignore_https_errors=False,
            )
            append_har_debug(game_dir, "browser_context_ok")
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            append_har_debug(game_dir, "page_created")

            def on_request(request: Any) -> None:
                nonlocal post_requests
                if _is_bgaming_state_post(request):
                    post_requests += 1
                    append_har_debug(
                        game_dir,
                        "provider_post_request",
                        index=post_requests,
                        method=str(request.method),
                        url=str(request.url),
                        resource_type=str(getattr(request, "resource_type", "") or ""),
                    )

            def on_response(response: Any) -> None:
                try:
                    request = response.request
                    if _is_bgaming_state_post(request):
                        append_har_debug(
                            game_dir,
                            "provider_post_response",
                            status=int(response.status),
                            url=str(response.url),
                        )
                except Exception:
                    return

            def on_request_failed(request: Any) -> None:
                try:
                    if not _is_bgaming_url(str(request.url or "")):
                        return
                    append_har_debug(
                        game_dir,
                        "network_request_failed",
                        method=str(request.method),
                        url=str(request.url),
                        failure=str(request.failure or ""),
                    )
                except Exception:
                    return

            def on_console(message: Any) -> None:
                nonlocal console_events
                try:
                    level = str(message.type or "").casefold()
                    if level not in {"error", "warning"} or console_events >= 40:
                        return
                    console_events += 1
                    append_har_debug(
                        game_dir,
                        "console",
                        level=level,
                        text=str(message.text or ""),
                    )
                except Exception:
                    return

            def on_page_error(error: Any) -> None:
                append_har_debug(
                    game_dir,
                    "page_error",
                    error=f"{type(error).__name__}: {error}",
                )

            page.on("request", on_request)
            page.on("response", on_response)
            page.on("requestfailed", on_request_failed)
            page.on("console", on_console)
            page.on("pageerror", on_page_error)

            try:
                append_har_debug(game_dir, "navigation_start", url=launch_url)
                navigation = page.goto(
                    launch_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                append_har_debug(
                    game_dir,
                    "navigation_ok",
                    status=(int(navigation.status) if navigation is not None else None),
                    final_url=str(page.url or ""),
                )
                page.wait_for_timeout(2500)
                append_har_debug(
                    game_dir,
                    "bootstrap_wait_complete",
                    provider_posts=post_requests,
                    frames=_frame_summary(page),
                )
                if stop_event.is_set():
                    append_har_debug(game_dir, "capture_cancelled_after_navigation")
                    return post_requests

                clicked = _click_standard_entry_controls(page, game_dir)
                page.wait_for_timeout(1200)
                append_har_debug(
                    game_dir,
                    "entry_phase_complete",
                    clicked=clicked,
                    provider_posts=post_requests,
                    frames=_frame_summary(page),
                )

                baseline_posts = post_requests
                _attempt_provider_generic_spin(
                    page,
                    baseline_posts,
                    lambda: post_requests,
                    game_dir,
                )
                append_har_debug(
                    game_dir,
                    "interaction_complete",
                    provider_posts=post_requests,
                    new_provider_posts=post_requests - baseline_posts,
                )
            finally:
                append_har_debug(game_dir, "context_close_start")
                try:
                    context.close()
                    append_har_debug(game_dir, "context_close_ok")
                finally:
                    browser.close()
                    append_har_debug(game_dir, "browser_close_ok")
    except Exception as exc:
        append_har_debug(
            game_dir,
            "capture_exception",
            error=f"{type(exc).__name__}: {exc}",
            provider_posts=post_requests,
        )
        raise

    append_har_debug(
        game_dir,
        "capture_complete",
        provider_posts=post_requests,
        har_exists=target.is_file(),
        har_bytes=(target.stat().st_size if target.is_file() else 0),
    )
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
        append_har_debug(
            game_dir,
            "reuse_existing_har",
            path=str(relative),
            bytes=(existing.stat().st_size if existing.is_file() else 0),
        )
        return HARCaptureResult(
            path=existing,
            captured=False,
            skipped=True,
        )

    if stop_event.is_set():
        append_har_debug(game_dir, "capture_cancelled_before_start")
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
    debug_log = analysis_dir / "har-debug.jsonl"

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
        append_har_debug(
            game_dir,
            "har_promoted",
            path=target.name,
            bytes=target.stat().st_size,
            provider_posts=post_requests,
            elapsed_ms=elapsed_ms,
        )
        metadata.write_text(
            json.dumps(
                {
                    "schema": "tester-spin/bgaming-har-capture/v2",
                    "captured_at": utc_now_iso(),
                    "launch_url": sanitize_session_url(launch_url),
                    "path": str(target.name),
                    "bytes": target.stat().st_size,
                    "provider_post_requests": post_requests,
                    "elapsed_ms": elapsed_ms,
                    "interaction": "provider-generic-entry+canvas-space",
                    "debug_log": debug_log.name,
                    "spin_probe_observed_provider_post": post_requests > 0,
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
        if post_requests == 0:
            progress(
                f"[{game_name}] HAR diagnóstico: no se observó ningún POST BGaming; "
                "revisar analysis/har-debug.jsonl."
            )
        else:
            progress(
                f"[{game_name}] HAR diagnóstico persistido en "
                "analysis/har-debug.jsonl."
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
        append_har_debug(
            game_dir,
            "ensure_capture_failed",
            error=message,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )
        metadata.write_text(
            json.dumps(
                {
                    "schema": "tester-spin/bgaming-har-capture/v2",
                    "captured_at": utc_now_iso(),
                    "launch_url": sanitize_session_url(launch_url),
                    "error": message,
                    "debug_log": debug_log.name,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        progress(
            f"[{game_name}] HAR captura ERROR (la prueba continúa): {message}; "
            "ver analysis/har-debug.jsonl."
        )
        return HARCaptureResult(
            path=None,
            captured=False,
            skipped=False,
            error=message,
        )
