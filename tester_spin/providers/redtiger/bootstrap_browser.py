from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

from tester_spin.providers.redtiger.bootstrap import BootstrapEndpoints
from tester_spin.providers.redtiger.demo_auth import demo_page_url
from tester_spin.providers.redtiger.runtime import (
    RedTigerRuntime,
    feature_buys_from_settings,
    sanitize_payload,
    settings_result,
    stakes_from_settings,
)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _json_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Red Tiger {label}: JSON inválido: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Red Tiger {label}: se esperaba objeto JSON.")
    return value


def _currency_decimals(settings_response: dict[str, Any]) -> int:
    result = settings_result(settings_response)
    user = result.get("user")
    balance = user.get("balance") if isinstance(user, dict) else None
    if isinstance(balance, dict):
        for value in balance.values():
            if isinstance(value, str) and re.fullmatch(r"-?\d+\.\d+", value.strip()):
                return min(8, len(value.strip().split(".", 1)[1]))
    return 2


def _copy_browser_cookies(session: requests.Session, cookies: list[dict[str, Any]]) -> None:
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not name:
            continue
        domain = str(cookie.get("domain") or "") or None
        path = str(cookie.get("path") or "/")
        session.cookies.set(name, value, domain=domain, path=path)


def _runtime_session(
    *,
    cookies: list[dict[str, Any]],
    observed_headers: dict[str, str],
    user_agent: str,
    settings_url: str,
    launcher_url: str,
) -> requests.Session:
    session = requests.Session()
    _copy_browser_cookies(session, cookies)

    ignored = {
        "cookie",
        "content-length",
        "host",
        "connection",
        "accept-encoding",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
    }
    for key, value in observed_headers.items():
        if str(key).lower() in ignored:
            continue
        if isinstance(value, str) and value:
            session.headers[str(key)] = value

    parsed = urlparse(settings_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    session.headers.setdefault("User-Agent", user_agent)
    session.headers.setdefault("Accept", "application/json, text/plain, */*")
    session.headers.setdefault("Content-Type", "application/json")
    session.headers.setdefault("Origin", origin)
    session.headers.setdefault("Referer", launcher_url or origin + "/")
    return session


def _safe_trace_url(value: str) -> str:
    """Keep routing evidence without persisting query/session credentials."""
    try:
        parsed = urlparse(str(value or ""))
    except Exception:
        return ""
    path = re.sub(r";jsessionid=[^/?;]+", ";jsessionid=<redacted>", parsed.path, flags=re.I)
    parts: list[str] = []
    for part in path.split("/"):
        if len(part) > 64:
            parts.append("<opaque>")
        else:
            parts.append(part)
    return urlunparse((parsed.scheme, parsed.netloc, "/".join(parts), "", "", ""))


def _is_settings_response(response: Any) -> bool:
    try:
        request = response.request
        path = urlparse(str(response.url or "")).path.rstrip("/")
        return request.method.upper() == "POST" and path.endswith("/platform/game/settings")
    except Exception:
        return False


def _is_demo_token_response(response: Any, demo_token_url: str) -> bool:
    try:
        expected = urlparse(str(demo_token_url or ""))
        current = urlparse(str(response.url or ""))
        return (
            response.request.method.upper() == "POST"
            and current.netloc.casefold() == expected.netloc.casefold()
            and current.path.rstrip("/").endswith("/api/v1/oss/token/demo")
        )
    except Exception:
        return False


def bootstrap_game(
    public_url: str,
    table_id: str,
    *,
    timeout_s: float,
    artifact_dir: Path,
    endpoints: BootstrapEndpoints | None = None,
    progress: Callable[[str], None] | None = None,
) -> RedTigerRuntime:
    """Bootstrap Red Tiger in one official browser context, then switch to HTTP.

    The official /demo/<tableId> route owns token/demo authentication, entry
    navigation, cookies, popup/iframe creation and setup/config. We observe the
    resulting platform/game/settings request context-wide. No x-api-key, entry,
    JSESSIONID, gserver hostname, runtime gameId or session token is reconstructed.
    """
    table = str(table_id or "").strip()
    if not table:
        raise ValueError("Red Tiger bootstrap requiere tableId del catálogo.")

    cfg = endpoints or BootstrapEndpoints()
    timeout_ms = max(8_000, int(float(timeout_s) * 1000))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    launch_url = demo_page_url(public_url, table)

    if progress is not None:
        progress(
            "Red Tiger bootstrap: abriendo ruta demo oficial en una sola sesión "
            "y observando token → launcher → settings..."
        )

    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = None
    context = None
    trace: list[dict[str, Any]] = []
    settings_box: list[Any] = []
    demo_token_statuses: list[int] = []
    try:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(locale="en-GB")

        def on_response(response: Any) -> None:
            try:
                request = response.request
                url = str(response.url or "")
                parsed = urlparse(url)
                host = parsed.netloc.casefold()
                interesting = (
                    "redtiger" in host
                    or "cmsevo" in host
                    or "evo-games" in host
                    or "/platform/game/" in parsed.path
                )
                if interesting and len(trace) < 300:
                    trace.append(
                        {
                            "method": str(request.method or ""),
                            "status": int(response.status),
                            "resource_type": str(getattr(request, "resource_type", "") or ""),
                            "url": _safe_trace_url(url),
                        }
                    )
                if _is_demo_token_response(response, cfg.demo_token_url):
                    demo_token_statuses.append(int(response.status))
                if _is_settings_response(response) and not settings_box:
                    settings_box.append(response)
            except Exception:
                return

        context.on("response", on_response)
        page = context.new_page()
        page.goto(launch_url, wait_until="domcontentloaded", timeout=timeout_ms)

        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while not settings_box and time.monotonic() < deadline:
            pages = list(context.pages)
            if not pages:
                break
            waiter = pages[-1]
            try:
                waiter.wait_for_timeout(100)
            except Exception:
                # A popup can close while another page continues the launcher.
                continue

        _write_json(
            artifact_dir / "bootstrap-trace.json",
            {
                "launch_url": _safe_trace_url(launch_url),
                "demo_token_statuses": demo_token_statuses,
                "settings_observed": bool(settings_box),
                "pages": [_safe_trace_url(page.url) for page in context.pages],
                "responses": trace,
            },
        )

        if not settings_box:
            tail = [
                f"{item.get('status')} {item.get('method')} {item.get('url')}"
                for item in trace[-8:]
            ]
            token_note = demo_token_statuses[-1] if demo_token_statuses else "no observado"
            raise TimeoutError(
                "Red Tiger: la sesión demo no emitió platform/game/settings "
                f"en {timeout_ms} ms; token/demo={token_note}; "
                f"últimas respuestas={tail!r}. "
                "Ver bootstrap/bootstrap-trace.json para el recorrido sanitizado."
            )

        settings_response = settings_box[0]
        settings_request = settings_response.request
        request_payload = _json_object(settings_request.post_data or "{}", "settings request")
        response_payload = _json_object(settings_response.text(), "settings response")
        settings_result(response_payload)

        settings_url = str(settings_response.url or "")
        parsed_settings = urlparse(settings_url)
        suffix = "/platform/game/settings"
        if not parsed_settings.path.endswith(suffix):
            raise ValueError(f"Red Tiger settings URL inesperada: {settings_url}")
        base_path = parsed_settings.path[: -len(suffix)]
        base_url = f"{parsed_settings.scheme}://{parsed_settings.netloc}{base_path}"
        spin_url = base_url.rstrip("/") + "/platform/game/spin"

        referer = ""
        try:
            referer = str(settings_request.headers.get("referer") or "")
        except Exception:
            pass
        live_pages = list(context.pages)
        fallback_page_url = str(live_pages[-1].url or "") if live_pages else ""
        launcher_url = referer or fallback_page_url

        user_agent = ""
        for current_page in reversed(live_pages):
            try:
                user_agent = str(current_page.evaluate("() => navigator.userAgent") or "")
                if user_agent:
                    break
            except Exception:
                continue
        if not user_agent:
            user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
            )

        cookies = context.cookies()
        try:
            all_headers = settings_request.all_headers()
        except Exception:
            all_headers = dict(settings_request.headers or {})
        observed_headers = {str(key): str(value) for key, value in all_headers.items()}

        result = response_payload["result"]
        user = result["user"]
        runtime_token = str(user.get("token") or request_payload.get("token") or "").strip()
        session_id = str(user.get("sessionId") or request_payload.get("sessionId") or "").strip()
        game_id = str(request_payload.get("gameId") or "").strip()
        user_data = request_payload.get("userData")
        custom = request_payload.get("custom")
        if not runtime_token or not session_id or not game_id:
            raise ValueError("Red Tiger settings no resolvió token/sessionId/gameId.")
        if not isinstance(user_data, dict) or not isinstance(custom, dict):
            raise ValueError("Red Tiger settings request no contiene userData/custom objetos.")
        user_data = dict(user_data)
        if user.get("userId") is not None:
            user_data["userId"] = user["userId"]

        stakes, default_stake = stakes_from_settings(response_payload)
        feature_buys = feature_buys_from_settings(response_payload)
        runtime_http = _runtime_session(
            cookies=cookies,
            observed_headers=observed_headers,
            user_agent=user_agent,
            settings_url=settings_url,
            launcher_url=launcher_url,
        )
        runtime = RedTigerRuntime(
            session=runtime_http,
            settings_url=settings_url,
            spin_url=spin_url,
            launcher_url=launcher_url,
            game_id=game_id,
            session_id=session_id,
            token=runtime_token,
            user_data=user_data,
            custom=dict(custom),
            settings_request=request_payload,
            settings_response=response_payload,
            stakes=stakes,
            default_stake=default_stake,
            currency_decimals=_currency_decimals(response_payload),
            feature_buys=feature_buys,
        )

        _write_json(artifact_dir / "settings.request.json", sanitize_payload(request_payload))
        _write_json(artifact_dir / "settings.response.json", sanitize_payload(response_payload))
        _write_json(
            artifact_dir / "runtime-profile.json",
            {
                "table_id": table,
                "game_id": game_id,
                "settings_url": _safe_trace_url(settings_url),
                "spin_url": _safe_trace_url(spin_url),
                "stakes": [str(value) for value in stakes],
                "default_stake": str(default_stake),
                "currency_decimals": runtime.currency_decimals,
                "feature_buys": [
                    {"name": item.name, "multiplier": str(item.multiplier)}
                    for item in feature_buys
                ],
                "game_settings": {
                    key: result["game"].get(key)
                    for key in (
                        "cols",
                        "rows",
                        "ways",
                        "version",
                        "rtp",
                        "volatilityIndex",
                        "gameType",
                        "stateful",
                        "hasFeatureBuy",
                        "gameModes",
                        "mathModes",
                        "featureBuyVersion",
                    )
                },
            },
        )

        if progress is not None:
            progress(
                "Red Tiger bootstrap: settings observado en la misma sesión oficial; "
                "continuando por HTTP directo."
            )
        return runtime
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        playwright.stop()
