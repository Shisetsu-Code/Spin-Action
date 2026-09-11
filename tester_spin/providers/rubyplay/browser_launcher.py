from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urljoin, urlparse


def _is_launcher_url(value: str) -> bool:
    try:
        parsed = urlparse(str(value or ""))
    except Exception:
        return False
    return parsed.path.rstrip("/").endswith("/launcher")


def _is_doubled_rubyplay_gamename(value: str) -> bool:
    """Reject exact accidental concatenation such as rp_160rp_160.

    This is a provider-family integrity check, not a per-game map. We only flag
    the value when it is exactly two identical ``rp_*`` identifiers joined
    together; merely sharing a prefix is not enough.
    """
    text = str(value or "").strip()
    match = re.fullmatch(r"(rp_[A-Za-z0-9_-]+)\1", text, re.I)
    return bool(match)


def _semantic_launcher_key(value: str) -> tuple[str, ...] | None:
    """Return the complete demo-launch tuple or None for placeholders/malformed URLs."""
    if not _is_launcher_url(value):
        return None
    parsed = urlparse(value)
    params = parse_qs(parsed.query, keep_blank_values=True)
    names = ("gamename", "operator", "server_url", "currency", "mode")
    resolved: list[str] = []
    for name in names:
        values = [str(item) for item in params.get(name, []) if str(item)]
        unique = list(dict.fromkeys(values))
        if len(unique) != 1:
            return None
        resolved.append(unique[0])
    if _is_doubled_rubyplay_gamename(resolved[0]):
        return None
    lang_values = [str(item) for item in params.get("lang", []) if str(item)]
    lang_unique = list(dict.fromkeys(lang_values))
    if len(lang_unique) > 1:
        return None
    resolved.append(lang_unique[0] if lang_unique else "")
    return tuple(resolved)


def _pick_complete_launcher(candidates: list[str]) -> str | None:
    by_key: dict[tuple[str, ...], str] = {}
    for raw in candidates:
        value = str(raw or "").strip()
        key = _semantic_launcher_key(value)
        if key is None:
            continue
        by_key.setdefault(key, value)
    if not by_key:
        return None
    if len(by_key) != 1:
        preview = [value for value in by_key.values()][:4]
        raise ValueError(
            "RubyPlay demo: varios launchers completos incompatibles "
            f"(candidatos={preview!r})."
        )
    return next(iter(by_key.values()))


def _launch_browser(*, headless: bool = True):
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(
        locale="en-US",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
        ),
    )
    return playwright, browser, context


def resolve_demo_launcher_browser(public_url: str, *, timeout_s: float = 30.0) -> str:
    """Resolve the executable RubyPlay demo launcher through the official UI.

    The public game page is only discovery/bootstrap UI. When its static HTML
    exposes an incomplete launcher, Chromium follows the site's own Play Demo
    flow and we capture the resulting complete /launcher URL. Gameplay remains
    endpoint-first after this one discovery step.
    """
    timeout_ms = max(5_000, int(float(timeout_s) * 1000))
    observed: list[str] = []

    def remember(value: str) -> None:
        candidate = str(value or "").strip()
        if candidate and _is_launcher_url(candidate) and candidate not in observed:
            observed.append(candidate)

    playwright, browser, context = _launch_browser(headless=True)
    try:
        context.on("request", lambda request: remember(request.url))
        page = context.new_page()
        page.goto(public_url, wait_until="domcontentloaded", timeout=timeout_ms)

        def harvest() -> None:
            for current in list(context.pages):
                remember(current.url)
                for frame in current.frames:
                    remember(frame.url)
                try:
                    values = current.locator("*").evaluate_all(
                        """
                        els => els.flatMap(el => Array.from(el.attributes || [])
                          .map(attr => String(attr.value || ''))
                          .filter(value => value.includes('/launcher')))
                        """
                    )
                except Exception:
                    values = []
                for value in values or []:
                    try:
                        remember(urljoin(current.url or public_url, str(value)))
                    except Exception:
                        continue

        harvest()
        direct = _pick_complete_launcher(observed)
        if direct:
            return direct

        clicked = False
        patterns = [
            re.compile(r"^\s*play\s+demo\s*$", re.I),
            re.compile(r"^\s*play\s+for\s+free\s*$", re.I),
            re.compile(r"\bdemo\b", re.I),
        ]
        # First use semantic roles where the site exposes them.
        for pattern in patterns:
            for role in ("link", "button"):
                try:
                    locator = page.get_by_role(role, name=pattern)
                    count = min(locator.count(), 8)
                except Exception:
                    count = 0
                for index in range(count):
                    try:
                        item = locator.nth(index)
                        if not item.is_visible():
                            continue
                        item.click(timeout=min(timeout_ms, 8_000), no_wait_after=True)
                        clicked = True
                        break
                    except Exception:
                        continue
                if clicked:
                    break
            if clicked:
                break

        # Bricks/WordPress builds sometimes bind the click to a generic wrapper
        # (div/span) instead of an accessible button. Click the exact visible text
        # itself, then its nearest clickable ancestor. No generated element IDs are
        # used and no game names are hardcoded.
        if not clicked:
            for pattern in patterns[:2]:
                try:
                    locator = page.get_by_text(pattern, exact=True)
                    count = min(locator.count(), 10)
                except Exception:
                    count = 0
                for index in range(count):
                    item = locator.nth(index)
                    try:
                        if not item.is_visible():
                            continue
                    except Exception:
                        continue
                    try:
                        item.click(timeout=min(timeout_ms, 8_000), no_wait_after=True)
                        clicked = True
                        break
                    except Exception:
                        pass
                    try:
                        ancestor = item.locator(
                            "xpath=ancestor-or-self::*[self::a or self::button or @role='button' or @onclick][1]"
                        )
                        if ancestor.count() and ancestor.first.is_visible():
                            ancestor.first.click(
                                timeout=min(timeout_ms, 8_000),
                                no_wait_after=True,
                            )
                            clicked = True
                            break
                    except Exception:
                        continue
                if clicked:
                    break

        # Final generic clickable-text fallback.
        if not clicked:
            try:
                locator = page.locator(
                    "a,button,[role=button],[onclick]"
                ).filter(has_text=re.compile(r"\b(play\s+demo|play\s+for\s+free|demo)\b", re.I))
                for index in range(min(locator.count(), 10)):
                    try:
                        item = locator.nth(index)
                        if item.is_visible():
                            item.click(timeout=min(timeout_ms, 8_000), no_wait_after=True)
                            clicked = True
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        deadline = time.monotonic() + max(5.0, float(timeout_s))
        while time.monotonic() < deadline:
            harvest()
            resolved = _pick_complete_launcher(observed)
            if resolved:
                return resolved
            page.wait_for_timeout(250)

        details = observed[-8:]
        raise ValueError(
            "RubyPlay demo: no apareció un launcher ejecutable después de resolver "
            f"Play Demo (click={clicked}, launchers_observados={details!r})."
        )
    finally:
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass
        try:
            playwright.stop()
        except Exception:
            pass


def capture_init_contract_browser(
    launcher_url: str,
    *,
    timeout_s: float = 30.0,
) -> dict[str, object]:
    """Capture the official client's first gameserver ``action=init`` envelope.

    This is a bootstrap fallback for bundles whose static/minified math wiring is
    ambiguous or has changed. Only protocol-envelope fields are returned; session
    keys and funModeData stay inside the browser and are never reused. Endpoint
    execution continues with a fresh tester-owned session afterwards.
    """
    timeout_ms = max(5_000, int(float(timeout_s) * 1000))
    captured: dict[str, object] = {}

    def on_request(request) -> None:
        nonlocal captured
        if captured:
            return
        try:
            parsed = urlparse(request.url)
            if not parsed.path.rstrip("/").endswith("/gameserver/demo"):
                return
            payload = request.post_data_json
            if not isinstance(payload, dict):
                return
            if str(payload.get("action") or "").lower() != "init":
                return
            protocol = payload.get("v_protocol")
            math_version = payload.get("v_math")
            device_type = str(payload.get("device_type") or "desktop")
            if isinstance(protocol, bool) or not isinstance(protocol, int):
                return
            if isinstance(math_version, bool) or not isinstance(math_version, int):
                return
            captured = {
                "v_protocol": int(protocol),
                "v_math": int(math_version),
                "device_type": device_type,
            }
        except Exception:
            return

    playwright, browser, context = _launch_browser(headless=True)
    try:
        context.on("request", on_request)
        page = context.new_page()
        page.goto(launcher_url, wait_until="domcontentloaded", timeout=timeout_ms)
        deadline = time.monotonic() + max(5.0, float(timeout_s))
        while time.monotonic() < deadline and not captured:
            page.wait_for_timeout(100)
        if not captured:
            raise ValueError(
                "RubyPlay demo: el cliente oficial no emitió gameserver action=init "
                "durante el fallback de bootstrap."
            )
        return dict(captured)
    finally:
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass
        try:
            playwright.stop()
        except Exception:
            pass
