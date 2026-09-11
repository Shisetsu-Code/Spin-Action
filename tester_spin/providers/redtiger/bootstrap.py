from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from tester_spin.providers.redtiger.demo_auth import demo_page_url
from tester_spin.providers.redtiger.runtime import (
    RedTigerRuntime,
    feature_buys_from_settings,
    sanitize_payload,
    settings_result,
    stakes_from_settings,
)


DEFAULT_DEMO_TOKEN_URL = "https://jinx.prod.cmsevo.com/api/v1/oss/token/demo"
DEFAULT_ENTRY_ORIGIN = "https://fansite.evo-games.com"


@dataclass(frozen=True, slots=True)
class BootstrapEndpoints:
    """Compatibility/configuration boundary for the observed Red Tiger deployment.

    The runtime bootstrap intentionally does not reproduce ``token/demo`` itself.
    The official ``/demo/<tableId>`` frontend owns token issuance, redirects and
    cookies inside one browser context. These fields remain available for tests and
    future deployments without leaking provider-specific state into other modules.
    """

    demo_token_url: str = DEFAULT_DEMO_TOKEN_URL
    entry_origin: str = DEFAULT_ENTRY_ORIGIN


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
    """Infer wire money precision from the fresh settings balance, not a title rule."""
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

    # Reuse the official launcher's non-sensitive HTTP shape rather than cloning
    # browser conventions by hand. Transport-specific headers that requests owns
    # itself are excluded; cookies are transferred through the cookie jar above.
    ignored = {"cookie", "content-length", "host", "connection", "accept-encoding"}
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


def _is_settings_response(response: Any) -> bool:
    try:
        return (
            response.request.method.upper() == "POST"
            and urlparse(response.url).path.rstrip("/").endswith("/platform/game/settings")
        )
    except Exception:
        return False


def _trace_response(response: Any) -> dict[str, Any]:
    try:
        request = response.request
        return {
            "method": str(request.method or ""),
            "status": int(response.status),
            "url": str(response.url or ""),
            "resource_type": str(getattr(request, "resource_type", "") or ""),
        }
    except Exception:
        return {"url": str(getattr(response, "url", "") or "")}


def bootstrap_game(
    public_url: str,
    table_id: str,
    *,
    timeout_s: float,
    artifact_dir: Path,
    endpoints: BootstrapEndpoints | None = None,
    progress: Callable[[str], None] | None = None,
) -> RedTigerRuntime:
    """Create one fresh official demo session, then switch to direct HTTP.

    The browser owns the complete provider bootstrap chain:

    ``/demo/<tableId> -> token/demo -> entry -> launcher -> platform/game/settings``.

    We deliberately do not replay token issuance, x-api-key, entry URLs or session
    cookies ourselves. Once the official client emits ``settings``, its observed
    request, response, cookies and headers become the authority for direct spins.
    """
    table = str(table_id or "").strip()
    if not table:
        raise ValueError("Red Tiger bootstrap requiere tableId del catálogo.")

    # Keep the configuration object as an explicit provider boundary even though
    # the official browser route now owns token/entry resolution end-to-end.
    _ = endpoints or BootstrapEndpoints()
    timeout_ms = max(30_000, int(float(timeout_s) * 1000))
    settings_timeout_ms = max(60_000, timeout_ms)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    launch_url = demo_page_url(public_url, table)
    if progress is not None:
        progress(
            "Red Tiger bootstrap: abriendo ruta demo oficial en una única sesión "
            f"de navegador para tableId={table}."
        )

    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = None
    context = None
    response_trace: list[dict[str, Any]] = []
    console_trace: list[dict[str, str]] = []
    page_errors: list[str] = []
    page = None
    try:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(locale="en-GB")
        page = context.new_page()

        def on_response(response: Any) -> None:
            if len(response_trace) >= 300:
                return
            response_trace.append(_trace_response(response))

        def on_console(message: Any) -> None:
            if len(console_trace) >= 100:
                return
            try:
                console_trace.append({"type": str(message.type), "text": str(message.text)})
            except Exception:
                pass

        def on_page_error(error: Any) -> None:
            if len(page_errors) < 50:
                page_errors.append(str(error))

        page.on("response", on_response)
        page.on("console", on_console)
        page.on("pageerror", on_page_error)

        try:
            with page.expect_response(_is_settings_response, timeout=settings_timeout_ms) as pending_settings:
                page.goto(launch_url, wait_until="domcontentloaded", timeout=timeout_ms)
            settings_response = pending_settings.value
        except PlaywrightTimeoutError as exc:
            diagnostic = {
                "table_id": table,
                "launch_url": launch_url,
                "final_url": str(page.url or "") if page is not None else "",
                "responses": response_trace,
                "console": console_trace,
                "page_errors": page_errors,
            }
            _write_json(artifact_dir / "bootstrap-trace.json", diagnostic)
            recent = response_trace[-8:]
            recent_text = "; ".join(
                f"{item.get('status', '?')} {item.get('method', '')} {item.get('url', '')}"
                for item in recent
            )
            raise RuntimeError(
                "Red Tiger: la ruta demo oficial no emitió platform/game/settings "
                f"en {settings_timeout_ms / 1000:.0f}s. final_url={diagnostic['final_url']!r}; "
                f"últimas respuestas={recent_text or 'ninguna'}. "
                f"Diagnóstico: {artifact_dir / 'bootstrap-trace.json'}"
            ) from exc

        _write_json(
            artifact_dir / "bootstrap-trace.json",
            {
                "table_id": table,
                "launch_url": launch_url,
                "final_url": str(page.url or ""),
                "responses": response_trace,
                "console": console_trace,
                "page_errors": page_errors,
            },
        )

        settings_request = settings_response.request
        request_payload = _json_object(settings_request.post_data or "{}", "settings request")
        response_payload = _json_object(settings_response.text(), "settings response")
        settings_result(response_payload)

        settings_url = settings_response.url
        parsed_settings = urlparse(settings_url)
        suffix = "/platform/game/settings"
        if not parsed_settings.path.endswith(suffix):
            raise ValueError(f"Red Tiger settings URL inesperada: {settings_url}")
        base_path = parsed_settings.path[: -len(suffix)]
        base_url = f"{parsed_settings.scheme}://{parsed_settings.netloc}{base_path}"
        spin_url = base_url.rstrip("/") + "/platform/game/spin"
        launcher_url = str(settings_request.headers.get("referer") or page.url or "")
        user_agent = str(page.evaluate("() => navigator.userAgent") or "")
        cookies = context.cookies()
        observed_headers = {str(key): str(value) for key, value in settings_request.headers.items()}

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
                "settings_url": settings_url,
                "spin_url": spin_url,
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
