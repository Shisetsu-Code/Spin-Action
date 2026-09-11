from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

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


def bootstrap_game(
    public_url: str,
    table_id: str,
    *,
    timeout_s: float,
    artifact_dir: Path,
    endpoints: BootstrapEndpoints | None = None,
) -> RedTigerRuntime:
    """Create a fresh demo session through the official launcher, then switch to HTTP.

    The browser is bootstrap-only. It solves the provider/edge launcher flow and we
    observe the official ``platform/game/settings`` request instead of guessing
    gameId, gserver hostname, session fields, cookies or client versions. All game
    actions after bootstrap use the captured provider contract directly over HTTP.
    """
    table = str(table_id or "").strip()
    if not table:
        raise ValueError("Red Tiger bootstrap requiere tableId del catálogo.")
    cfg = endpoints or BootstrapEndpoints()
    timeout_ms = max(8_000, int(float(timeout_s) * 1000))
    artifact_dir.mkdir(parents=True, exist_ok=True)

    token_session = requests.Session()
    token_session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": f"{urlparse(public_url).scheme}://{urlparse(public_url).netloc}",
            "Referer": public_url,
        }
    )
    token_request = {
        "demo": {"language": "en-GB", "currency": "VC0"},
        "game": {"tableId": table},
    }
    token_response = token_session.post(
        cfg.demo_token_url,
        json=token_request,
        timeout=timeout_s,
    )
    token_response.raise_for_status()
    token_data = _json_object(token_response.text, "demo token")
    entry = str(token_data.get("entry") or "").strip()
    if not entry:
        raise ValueError("Red Tiger demo token no devolvió entry.")
    entry_url = urljoin(cfg.entry_origin.rstrip("/") + "/", entry.lstrip("/"))

    _write_json(artifact_dir / "demo-token.request.json", token_request)
    _write_json(
        artifact_dir / "demo-token.response.json",
        {
            "entry_present": bool(token_data.get("entry")),
            "entry_embedded_present": bool(token_data.get("entryEmbedded")),
        },
    )

    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = None
    context = None
    try:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(locale="en-GB")
        page = context.new_page()
        with page.expect_response(
            lambda response: (
                response.request.method.upper() == "POST"
                and urlparse(response.url).path.rstrip("/").endswith("/platform/game/settings")
            ),
            timeout=timeout_ms,
        ) as pending_settings:
            page.goto(entry_url, wait_until="domcontentloaded", timeout=timeout_ms)

        settings_response = pending_settings.value
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

        _write_json(
            artifact_dir / "settings.request.json",
            sanitize_payload(request_payload),
        )
        _write_json(
            artifact_dir / "settings.response.json",
            sanitize_payload(response_payload),
        )
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
        return runtime
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        playwright.stop()
