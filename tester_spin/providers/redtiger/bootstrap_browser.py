from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlparse, urlunparse

import requests

from tester_spin.providers.redtiger.bootstrap import BootstrapEndpoints
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
        name = str(key)
        lowered = name.casefold()
        if not name or name.startswith(":") or lowered in ignored:
            continue
        if any(ch in name for ch in "\r\n\t "):
            continue
        if isinstance(value, str) and value:
            session.headers[name] = value

    parsed = urlparse(settings_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    session.headers.setdefault("User-Agent", user_agent)
    session.headers.setdefault("Accept", "application/json, text/plain, */*")
    session.headers.setdefault("Content-Type", "application/json")
    session.headers.setdefault("Origin", origin)
    session.headers.setdefault("Referer", launcher_url or origin + "/")
    return session


def _safe_trace_url(value: str) -> str:
    """Preserve routing/query shape while redacting all credential values."""
    try:
        parsed = urlparse(str(value or ""))
    except Exception:
        return ""
    path = re.sub(r";jsessionid=[^/?;]+", ";jsessionid=<redacted>", parsed.path, flags=re.I)
    parts: list[str] = []
    for part in path.split("/"):
        parts.append("<opaque>" if len(part) > 64 else part)
    query_keys = sorted({key for key, _value in parse_qsl(parsed.query, keep_blank_values=True) if key})
    query = "&".join(f"{key}=<redacted>" for key in query_keys)
    return urlunparse((parsed.scheme, parsed.netloc, "/".join(parts), "", query, ""))


def _header_shape(headers: dict[str, str]) -> dict[str, Any]:
    names = sorted({str(key).casefold() for key in (headers or {})})
    cookie_names: list[str] = []
    cookie_value = ""
    for key, value in (headers or {}).items():
        if str(key).casefold() == "cookie":
            cookie_value = str(value or "")
            break
    if cookie_value:
        cookie_names = sorted(
            {
                chunk.split("=", 1)[0].strip()
                for chunk in cookie_value.split(";")
                if "=" in chunk and chunk.split("=", 1)[0].strip()
            }
        )
    return {"header_names": names, "cookie_names": cookie_names}


def _embedded_entry_url(entries: dict[str, str], entry_origin: str, denied_url: str = "") -> str:
    """Legacy helper kept for old diagnostic fixtures; active flow does not invent entry URLs."""
    raw = str(entries.get("entryEmbedded") or "").strip()
    if not raw:
        return ""
    candidate = urljoin(entry_origin.rstrip("/") + "/", raw.lstrip("/"))
    if denied_url and candidate == denied_url:
        return ""
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return candidate


def _bootstrap_timeouts(timeout_s: float) -> tuple[int, int, int]:
    requested_ms = max(15_000, int(max(1.0, float(timeout_s)) * 1000))
    navigation_ms = min(15_000, requested_ms)
    total_ms = requested_ms
    post_json_grace_ms = min(10_000, max(5_000, navigation_ms))
    return navigation_ms, total_ms, post_json_grace_ms


def _stop_requested(stop_event: Any | None) -> bool:
    try:
        return bool(stop_event is not None and stop_event.is_set())
    except Exception:
        return False


def _raise_if_stopped(stop_event: Any | None) -> None:
    if _stop_requested(stop_event):
        raise InterruptedError("Detención solicitada durante bootstrap Red Tiger.")


def _goto_commit(
    page: Any,
    url: str,
    *,
    timeout_ms: int,
    stop_event: Any | None,
    referer: str | None = None,
) -> Any:
    _raise_if_stopped(stop_event)
    response = page.goto(
        url,
        wait_until="commit",
        timeout=max(1_000, min(3_000, int(timeout_ms))),
        referer=referer,
    )
    _raise_if_stopped(stop_event)
    return response


def _browser_user_agent(browser: Any) -> str:
    version = str(getattr(browser, "version", "") or "").strip()
    if version and re.fullmatch(r"\d+(?:\.\d+){1,3}", version):
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version} Safari/537.36"
        )
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
    )


def _launch_browser(playwright: Any) -> tuple[Any, str, list[str]]:
    failures: list[str] = []
    try:
        return playwright.chromium.launch(channel="chrome", headless=True), "system-chrome-headless", failures
    except Exception as exc:
        failures.append(f"system-chrome-headless: {type(exc).__name__}: {exc}")
    browser = playwright.chromium.launch(headless=True)
    return browser, "playwright-chromium-headless", failures


def _is_settings_response(response: Any) -> bool:
    try:
        request = response.request
        path = urlparse(str(response.url or "")).path.rstrip("/")
        return request.method.upper() == "POST" and path.endswith("/platform/game/settings")
    except Exception:
        return False


def _is_start_response(response: Any) -> bool:
    try:
        parsed = urlparse(str(response.url or ""))
        return (
            response.request.method.upper() == "GET"
            and parsed.netloc.casefold() == "games.evolution.com"
            and parsed.path.rstrip("/") == "/wp-json/games/v1/start"
        )
    except Exception:
        return False


def _is_config_response(response: Any) -> bool:
    try:
        parsed = urlparse(str(response.url or ""))
        return response.request.method.upper() == "GET" and parsed.path.rstrip("/").endswith("/config")
    except Exception:
        return False


def _wait_for_official_loader(
    page: Any,
    *,
    expected_launch_id: str,
    timeout_ms: int,
    stop_event: Any | None,
) -> str:
    deadline = time.monotonic() + min(8.0, max(2.0, timeout_ms / 1000.0))
    last_id = ""
    while time.monotonic() < deadline:
        _raise_if_stopped(stop_event)
        try:
            state = page.evaluate(
                """() => {
                    const root = document.querySelector('.game-playable');
                    const button = document.querySelector('#start-game');
                    return {
                        id: root && root.dataset ? String(root.dataset.gameId || '') : '',
                        button: !!button,
                        loader: typeof window.EvolutionGameLoader === 'function'
                    };
                }"""
            )
        except Exception:
            state = None
        if isinstance(state, dict):
            last_id = str(state.get("id") or "").strip()
            if last_id and last_id != expected_launch_id:
                raise ValueError(
                    f"Evolution Games: post id del DOM={last_id!r} != catálogo={expected_launch_id!r}."
                )
            if last_id == expected_launch_id and state.get("button") and state.get("loader"):
                return last_id
        page.wait_for_timeout(100)
    raise TimeoutError(
        "Evolution Games: la página no inicializó #start-game/EvolutionGameLoader "
        f"para post id={expected_launch_id!r}; último id={last_id!r}."
    )


def _submit_official_start(page: Any, *, stop_event: Any | None) -> None:
    _raise_if_stopped(stop_event)
    page.evaluate(
        """() => {
            const form = document.querySelector('form.start-form');
            const button = document.querySelector('#start-game');
            if (!form || !button) throw new Error('official start form missing');
            if (typeof form.requestSubmit === 'function') form.requestSubmit(button);
            else button.click();
        }"""
    )
    _raise_if_stopped(stop_event)


def bootstrap_game(
    public_url: str,
    table_id: str,
    *,
    timeout_s: float,
    artifact_dir: Path,
    endpoints: BootstrapEndpoints | None = None,
    progress: Callable[[str], None] | None = None,
    stop_event: Any | None = None,
) -> RedTigerRuntime:
    """Launch the official Evolution Games page, observe settings, then use HTTP directly.

    The captured public flow is:
    /slots/<slug>/ -> GET /wp-json/games/v1/start?id=<WP post id>&mobile=false
    -> showcase.evo-games.com iframe -> /setup -> /config -> Red Tiger launcher
    -> POST /platform/game/settings.

    Tester-Spin lets the site's own JavaScript perform the nonce-protected start
    request instead of copying X-WP-Nonce or session credentials from a HAR.
    """
    launch_id = str(table_id or "").strip()
    if not launch_id:
        raise ValueError("Red Tiger bootstrap requiere Evolution post id del catálogo.")
    parsed_public = urlparse(str(public_url or ""))
    if parsed_public.netloc.casefold() != "games.evolution.com":
        raise ValueError("Red Tiger bootstrap activo requiere games.evolution.com.")
    if not re.fullmatch(r"/slots/[^/]+/?", parsed_public.path or ""):
        raise ValueError("Red Tiger bootstrap requiere URL pública /slots/<slug>/.")

    _raise_if_stopped(stop_event)
    _ = endpoints  # compatibility with the provider constructor; no fixed launch endpoints are used.
    navigation_timeout_ms, settings_timeout_ms, _post_json_grace_ms = _bootstrap_timeouts(timeout_s)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    if progress is not None:
        progress(
            "Red Tiger bootstrap: abriendo games.evolution.com y usando el formulario "
            "oficial para start → showcase → config → launcher → settings..."
        )

    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = None
    context = None
    trace: list[dict[str, Any]] = []
    failed_requests: list[dict[str, str]] = []
    console_trace: list[dict[str, str]] = []
    page_errors: list[str] = []
    settings_box: list[Any] = []
    start_statuses: list[int] = []
    start_shape: dict[str, Any] = {}
    config_table_ids: list[str] = []
    browser_profile = ""
    browser_launch_failures: list[str] = []

    try:
        _raise_if_stopped(stop_event)
        browser, browser_profile, browser_launch_failures = _launch_browser(playwright)
        context = browser.new_context(
            locale="en-GB",
            user_agent=_browser_user_agent(browser),
            viewport={"width": 1365, "height": 900},
        )
        if progress is not None:
            progress(
                f"Red Tiger bootstrap: navegador={browser_profile}; "
                f"timeout_total={settings_timeout_ms // 1000}s."
            )

        def on_response(response: Any) -> None:
            try:
                request = response.request
                url = str(response.url or "")
                parsed = urlparse(url)
                host = parsed.netloc.casefold()
                interesting = (
                    host == "games.evolution.com"
                    or "evo-games.com" in host
                    or "redtiger" in host
                    or "/platform/game/" in parsed.path
                )
                if interesting and len(trace) < 600:
                    trace.append(
                        {
                            "method": str(request.method or ""),
                            "status": int(response.status),
                            "resource_type": str(getattr(request, "resource_type", "") or ""),
                            "url": _safe_trace_url(url),
                        }
                    )

                if _is_start_response(response):
                    start_statuses.append(int(response.status))
                    if int(response.status) < 400:
                        try:
                            payload = response.json()
                        except Exception:
                            payload = None
                        if isinstance(payload, dict):
                            launch_payload = str(payload.get("payload") or "")
                            launch_parsed = urlparse(launch_payload)
                            start_shape.update(
                                {
                                    "success": payload.get("success") is True,
                                    "payload_host": launch_parsed.netloc,
                                    "payload_path": launch_parsed.path,
                                    "payload_query_keys": sorted(
                                        key for key, _value in parse_qsl(launch_parsed.query, keep_blank_values=True)
                                    ),
                                }
                            )

                if _is_config_response(response) and int(response.status) < 400:
                    try:
                        payload = response.json()
                    except Exception:
                        payload = None
                    if isinstance(payload, dict):
                        observed = str(payload.get("table_id") or "").strip()
                        if observed and observed not in config_table_ids:
                            config_table_ids.append(observed)

                if _is_settings_response(response) and not settings_box:
                    settings_box.append(response)
            except Exception:
                return

        def on_request_failed(request: Any) -> None:
            if len(failed_requests) >= 120:
                return
            try:
                failed_requests.append(
                    {
                        "method": str(request.method or ""),
                        "url": _safe_trace_url(str(request.url or "")),
                        "failure": str(request.failure or ""),
                    }
                )
            except Exception:
                return

        def attach_page(current_page: Any) -> None:
            def on_console(message: Any) -> None:
                if len(console_trace) < 120:
                    try:
                        console_trace.append(
                            {"type": str(message.type or ""), "text": str(message.text or "")[:1000]}
                        )
                    except Exception:
                        pass

            def on_page_error(error: Any) -> None:
                if len(page_errors) < 60:
                    page_errors.append(str(error)[:2000])

            current_page.on("console", on_console)
            current_page.on("pageerror", on_page_error)

        context.on("response", on_response)
        context.on("requestfailed", on_request_failed)
        context.on("page", attach_page)
        page = context.new_page()
        attach_page(page)

        try:
            _goto_commit(
                page,
                public_url,
                timeout_ms=navigation_timeout_ms,
                stop_event=stop_event,
            )
        except InterruptedError:
            raise
        except Exception as exc:
            if progress is not None:
                progress(
                    "Red Tiger bootstrap: navegación pública no terminó limpia "
                    f"({type(exc).__name__}); esperando DOM oficial..."
                )

        _wait_for_official_loader(
            page,
            expected_launch_id=launch_id,
            timeout_ms=navigation_timeout_ms,
            stop_event=stop_event,
        )
        if progress is not None:
            progress(
                f"Red Tiger bootstrap: post id={launch_id} validado; ejecutando Launch game oficial."
            )
        _submit_official_start(page, stop_event=stop_event)

        deadline = time.monotonic() + (settings_timeout_ms / 1000.0)
        while not settings_box and time.monotonic() < deadline:
            _raise_if_stopped(stop_event)
            if start_statuses and start_statuses[-1] >= 400:
                break
            if start_shape and start_shape.get("success") is False:
                break
            pages = list(context.pages)
            if not pages:
                break
            try:
                pages[-1].wait_for_timeout(100)
            except Exception:
                continue

        _raise_if_stopped(stop_event)
        diagnostic = {
            "source": "games.evolution.com",
            "public_url": _safe_trace_url(public_url),
            "launch_id": launch_id,
            "browser_profile": browser_profile,
            "browser_launch_failures": browser_launch_failures,
            "timeouts_ms": {
                "navigation": navigation_timeout_ms,
                "total": settings_timeout_ms,
                "non_interruptible_navigation_slice": min(3_000, navigation_timeout_ms),
            },
            "start_statuses": start_statuses,
            "start_shape": start_shape,
            "config_table_ids": config_table_ids,
            "settings_observed": bool(settings_box),
            "pages": [_safe_trace_url(current.url) for current in context.pages],
            "responses": trace,
            "failed_requests": failed_requests,
            "console": console_trace,
            "page_errors": page_errors,
        }
        _write_json(artifact_dir / "bootstrap-trace.json", diagnostic)

        if not settings_box:
            tail = [
                f"{item.get('status')} {item.get('method')} {item.get('url')}"
                for item in trace[-12:]
            ]
            failed_tail = [
                f"{item.get('method')} {item.get('url')} => {item.get('failure')}"
                for item in failed_requests[-5:]
            ]
            raise TimeoutError(
                "Red Tiger: Evolution Games no emitió platform/game/settings; "
                f"start={start_statuses[-3:]!r}, start_shape={start_shape!r}, "
                f"config_table_ids={config_table_ids!r}, últimas respuestas={tail!r}, "
                f"fallos={failed_tail!r}. Ver bootstrap/bootstrap-trace.json."
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
            user_agent = _browser_user_agent(browser)

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
                "launch_id": launch_id,
                "observed_table_id": config_table_ids[-1] if config_table_ids else "",
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
            observed_table = config_table_ids[-1] if config_table_ids else "—"
            progress(
                "Red Tiger bootstrap: settings observado vía Evolution Games; "
                f"tableId={observed_table}, gameId={game_id}; continuando por HTTP directo."
            )
        return runtime
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
        playwright.stop()
