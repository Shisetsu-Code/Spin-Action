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
from tester_spin.providers.redtiger.demo_auth import demo_page_url
from tester_spin.providers.redtiger.evo_auth import resolve_json_entry_auth
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


def _response_body_hint(response: Any) -> str:
    try:
        text = str(response.text() or "")[:2000]
    except Exception:
        return ""
    text = re.sub(r"[A-Za-z0-9_.~-]{40,}", "<opaque>", text)
    return re.sub(r"\s+", " ", text).strip()[:1200]


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
    """Prefer installed Chrome; fall back to Playwright Chromium without failing bootstrap."""
    failures: list[str] = []
    try:
        return playwright.chromium.launch(channel="chrome", headless=True), "system-chrome-headless", failures
    except Exception as exc:
        failures.append(f"system-chrome-headless: {type(exc).__name__}: {exc}")
    browser = playwright.chromium.launch(headless=True)
    return browser, "playwright-chromium-headless", failures


def _embedded_entry_url(entries: dict[str, str], entry_origin: str, denied_url: str = "") -> str:
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
    """Return per-navigation, total bootstrap and post-JSON grace budgets.

    The GUI/provider already supplies a per-game timeout. Historically Red Tiger
    forced every navigation to at least 30 seconds and the whole bootstrap to at
    least 60 seconds, which made deterministic launcher rejections painfully slow
    across hundreds of games. Keep the caller's total budget, but prevent one
    browser step from monopolizing it and give a resolved JSON loader a short grace
    window to emit settings.
    """
    requested_ms = max(15_000, int(max(1.0, float(timeout_s)) * 1000))
    navigation_ms = min(15_000, requested_ms)
    total_ms = requested_ms
    post_json_grace_ms = min(10_000, max(5_000, navigation_ms))
    return navigation_ms, total_ms, post_json_grace_ms


def bootstrap_game(
    public_url: str,
    table_id: str,
    *,
    timeout_s: float,
    artifact_dir: Path,
    endpoints: BootstrapEndpoints | None = None,
    progress: Callable[[str], None] | None = None,
) -> RedTigerRuntime:
    """Bootstrap Red Tiger in one provider-owned browser context, then use HTTP directly.

    No title, runtime gameId, gserver host, API key or session credential is fixed.
    The official demo route owns token issuance. We first allow its normal entry
    navigation, then its advertised embedded entry, and finally the Evolution JSON
    entry contract proven by the live client. All opaque session values still come
    from the current token/demo response.
    """
    table = str(table_id or "").strip()
    if not table:
        raise ValueError("Red Tiger bootstrap requiere tableId del catálogo.")

    cfg = endpoints or BootstrapEndpoints()
    navigation_timeout_ms, settings_timeout_ms, post_json_grace_ms = _bootstrap_timeouts(timeout_s)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    launch_url = demo_page_url(public_url, table)

    if progress is not None:
        progress(
            "Red Tiger bootstrap: abriendo ruta demo oficial y observando "
            "token → entry → launcher → settings..."
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
    demo_token_statuses: list[int] = []
    demo_entries: dict[str, str] = {}
    entry_responses: list[Any] = []
    embedded_fallback_attempted = False
    json_auth_attempted = False
    json_auth_diagnostic: dict[str, Any] = {}
    browser_profile = ""
    browser_launch_failures: list[str] = []
    terminal_bootstrap_failure = False

    try:
        browser, browser_profile, browser_launch_failures = _launch_browser(playwright)
        context = browser.new_context(
            locale="en-GB",
            user_agent=_browser_user_agent(browser),
            viewport={"width": 1365, "height": 900},
        )

        if progress is not None:
            progress(
                f"Red Tiger bootstrap: navegador={browser_profile}; "
                f"timeout_total={settings_timeout_ms // 1000}s, paso={navigation_timeout_ms // 1000}s."
            )

        entry_host = urlparse(cfg.entry_origin).netloc.casefold()

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
                if interesting and len(trace) < 500:
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
                    if int(response.status) < 400:
                        try:
                            payload = response.json()
                        except Exception:
                            payload = None
                        if isinstance(payload, dict):
                            for key in ("entry", "entryEmbedded"):
                                value = str(payload.get(key) or "").strip()
                                if value:
                                    demo_entries[key] = value

                if host == entry_host and parsed.path.rstrip("/").endswith("/entry"):
                    entry_responses.append(response)

                if _is_settings_response(response) and not settings_box:
                    settings_box.append(response)
            except Exception:
                return

        def on_request_failed(request: Any) -> None:
            if len(failed_requests) >= 120:
                return
            try:
                failure = request.failure
                failed_requests.append(
                    {
                        "method": str(request.method or ""),
                        "url": _safe_trace_url(str(request.url or "")),
                        "failure": str(failure or ""),
                    }
                )
            except Exception:
                return

        def attach_page(current_page: Any) -> None:
            def on_console(message: Any) -> None:
                if len(console_trace) >= 120:
                    return
                try:
                    console_trace.append(
                        {"type": str(message.type or ""), "text": str(message.text or "")[:1000]}
                    )
                except Exception:
                    return

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
            page.goto(launch_url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)
        except Exception as navigation_exc:
            if progress is not None:
                progress(
                    "Red Tiger bootstrap: navegación principal no terminó limpia "
                    f"({type(navigation_exc).__name__}); observando el contexto completo..."
                )

        deadline = time.monotonic() + (settings_timeout_ms / 1000.0)
        while not settings_box and time.monotonic() < deadline:
            denied = next(
                (
                    response
                    for response in reversed(entry_responses)
                    if int(getattr(response, "status", 0) or 0) >= 400
                ),
                None,
            )
            if denied is not None and not embedded_fallback_attempted:
                denied_url = str(getattr(denied, "url", "") or "")
                embedded_url = _embedded_entry_url(demo_entries, cfg.entry_origin, denied_url)
                embedded_fallback_attempted = True
                if embedded_url:
                    if progress is not None:
                        progress(
                            "Red Tiger bootstrap: entry principal falló; probando "
                            "entryEmbedded anunciado por token/demo en la misma sesión..."
                        )
                    fallback_page = context.new_page()
                    attach_page(fallback_page)
                    try:
                        fallback_page.goto(
                            embedded_url,
                            wait_until="domcontentloaded",
                            timeout=navigation_timeout_ms,
                            referer=launch_url,
                        )
                    except Exception as fallback_exc:
                        if progress is not None:
                            progress(
                                "Red Tiger bootstrap: entryEmbedded no terminó navegación limpia "
                                f"({type(fallback_exc).__name__}); seguimos observando settings..."
                            )

            latest_entry_failure = next(
                (
                    response
                    for response in reversed(entry_responses)
                    if int(getattr(response, "status", 0) or 0) >= 400
                ),
                None,
            )
            if (
                latest_entry_failure is not None
                and embedded_fallback_attempted
                and not json_auth_attempted
                and str(demo_entries.get("entry") or "").strip()
            ):
                json_auth_attempted = True
                if progress is not None:
                    progress(
                        "Red Tiger bootstrap: navegación entry/embedded falló; resolviendo "
                        "el contrato JSON del cliente Evolution con client_version vivo..."
                    )
                try:
                    target_url, json_auth_diagnostic = resolve_json_entry_auth(
                        context,
                        entry=demo_entries["entry"],
                        entry_origin=cfg.entry_origin,
                        referer=launch_url,
                        timeout_ms=navigation_timeout_ms,
                    )
                    json_page = context.new_page()
                    attach_page(json_page)
                    try:
                        json_page.goto(
                            target_url,
                            wait_until="domcontentloaded",
                            timeout=navigation_timeout_ms,
                            referer=launch_url,
                        )
                    except Exception as json_navigation_exc:
                        if progress is not None:
                            progress(
                                "Red Tiger bootstrap: loader resuelto por auth JSON no terminó navegación limpia "
                                f"({type(json_navigation_exc).__name__}); damos una gracia corta a settings..."
                            )
                    deadline = min(
                        deadline,
                        time.monotonic() + (post_json_grace_ms / 1000.0),
                    )
                except Exception as json_auth_exc:
                    json_auth_diagnostic = {
                        "error": f"{type(json_auth_exc).__name__}: {json_auth_exc}"
                    }
                    terminal_bootstrap_failure = True
                    if progress is not None:
                        progress(
                            "Red Tiger bootstrap: auth JSON Evolution no pudo resolverse: "
                            f"{type(json_auth_exc).__name__}: {json_auth_exc}; "
                            "no quedan rutas de bootstrap, fallando sin esperar el timeout completo."
                        )

            if terminal_bootstrap_failure:
                break

            pages = list(context.pages)
            if not pages:
                break
            try:
                pages[-1].wait_for_timeout(100)
            except Exception:
                continue

        entry_diagnostics: list[dict[str, Any]] = []
        for response in entry_responses[-8:]:
            try:
                request = response.request
                try:
                    req_headers = request.all_headers()
                except Exception:
                    req_headers = dict(request.headers or {})
                try:
                    res_headers = response.all_headers()
                except Exception:
                    res_headers = {}
                item: dict[str, Any] = {
                    "status": int(response.status),
                    "url": _safe_trace_url(str(response.url or "")),
                    "request": _header_shape({str(k): str(v) for k, v in req_headers.items()}),
                    "response_header_names": sorted(str(key).casefold() for key in res_headers),
                }
                if int(response.status) >= 400:
                    item["body_hint"] = _response_body_hint(response)
                entry_diagnostics.append(item)
            except Exception:
                continue

        diagnostic = {
            "launch_url": _safe_trace_url(launch_url),
            "browser_profile": browser_profile,
            "browser_launch_failures": browser_launch_failures,
            "timeouts_ms": {
                "navigation": navigation_timeout_ms,
                "total": settings_timeout_ms,
                "post_json_grace": post_json_grace_ms,
            },
            "terminal_bootstrap_failure": terminal_bootstrap_failure,
            "demo_token_statuses": demo_token_statuses,
            "demo_entry_fields": sorted(demo_entries),
            "embedded_fallback_attempted": embedded_fallback_attempted,
            "json_auth_attempted": json_auth_attempted,
            "json_auth": json_auth_diagnostic,
            "settings_observed": bool(settings_box),
            "pages": [_safe_trace_url(current.url) for current in context.pages],
            "entry_attempts": entry_diagnostics,
            "responses": trace,
            "failed_requests": failed_requests,
            "console": console_trace,
            "page_errors": page_errors,
        }
        _write_json(artifact_dir / "bootstrap-trace.json", diagnostic)

        if not settings_box:
            tail = [
                f"{item.get('status')} {item.get('method')} {item.get('url')}"
                for item in trace[-10:]
            ]
            failed_tail = [
                f"{item.get('method')} {item.get('url')} => {item.get('failure')}"
                for item in failed_requests[-5:]
            ]
            entry_tail = [
                f"{item.get('status')} {item.get('url')} cookies={item.get('request', {}).get('cookie_names', [])}"
                for item in entry_diagnostics[-4:]
            ]
            token_note = demo_token_statuses[-1] if demo_token_statuses else "no observado"
            json_note = json_auth_diagnostic or {"attempted": json_auth_attempted}
            reason = "rutas agotadas" if terminal_bootstrap_failure else "timeout"
            raise TimeoutError(
                "Red Tiger: la sesión demo no emitió platform/game/settings "
                f"({reason}); presupuesto={settings_timeout_ms} ms; navegador={browser_profile}; "
                f"token/demo={token_note}; entry={entry_tail!r}; json_auth={json_note!r}; "
                f"últimas respuestas={tail!r}; fallos={failed_tail!r}. "
                "Ver bootstrap/bootstrap-trace.json para diagnóstico sanitizado."
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
                "Red Tiger bootstrap: settings observado en la sesión oficial; "
                "continuando por HTTP directo."
            )
        return runtime
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        playwright.stop()
