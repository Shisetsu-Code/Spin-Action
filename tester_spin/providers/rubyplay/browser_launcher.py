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


def _semantic_launcher_key(value: str) -> tuple[str, ...] | None:
    """Return the complete demo-launch tuple or None for placeholders.

    Public RubyPlay game pages can expose an incomplete launcher before the user
    presses Play Demo. The runtime must not treat that placeholder as executable.
    Exact duplicate query values are tolerated, but conflicting values are not.
    """
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


def resolve_demo_launcher_browser(public_url: str, *, timeout_s: float = 30.0) -> str:
    """Resolve the executable RubyPlay demo launcher through the official UI.

    The public game page is only discovery/bootstrap UI. When its static HTML
    exposes an incomplete launcher, Chromium follows the site's own Play Demo
    flow and we capture the resulting complete /launcher URL. Gameplay remains
    endpoint-first after this one discovery step.
    """
    from playwright.sync_api import sync_playwright

    timeout_ms = max(5_000, int(float(timeout_s) * 1000))
    observed: list[str] = []

    def remember(value: str) -> None:
        candidate = str(value or "").strip()
        if candidate and _is_launcher_url(candidate) and candidate not in observed:
            observed.append(candidate)

    playwright = sync_playwright().start()
    browser = None
    context = None
    try:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
            ),
        )
        context.on("request", lambda request: remember(request.url))
        page = context.new_page()
        page.goto(public_url, wait_until="domcontentloaded", timeout=timeout_ms)

        def harvest() -> None:
            assert context is not None
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

        # Prefer the site's semantic Play Demo control. No generated CSS/Bricks
        # IDs or game names are used here.
        clicked = False
        patterns = [re.compile(r"^\s*play\s+demo\s*$", re.I), re.compile(r"\bdemo\b", re.I)]
        for pattern in patterns:
            for role in ("link", "button"):
                try:
                    locator = page.get_by_role(role, name=pattern)
                    count = min(locator.count(), 6)
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

        # Some builds attach the launcher to data-* / onclick rather than an
        # accessible link. Only use a text match as the final generic fallback.
        if not clicked:
            try:
                locator = page.locator("a,button,[role=button]").filter(
                    has_text=re.compile(r"\bplay\s+demo\b", re.I)
                )
                for index in range(min(locator.count(), 6)):
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

        details = observed[-6:]
        raise ValueError(
            "RubyPlay demo: no apareció un launcher ejecutable después de resolver "
            f"Play Demo (click={clicked}, launchers_observados={details!r})."
        )
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        try:
            playwright.stop()
        except Exception:
            pass
