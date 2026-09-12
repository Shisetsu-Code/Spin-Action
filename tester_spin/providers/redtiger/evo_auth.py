from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse


_FRONTEND_PATHS = ("/frontend/evo/r3/",)


def client_version_from_html(html: str) -> str:
    """Read the live Evolution client build from its HTML meta tag."""
    text = str(html or "")
    for tag in re.findall(r"<meta\b[^>]*>", text, flags=re.I):
        name = re.search(r"\bname\s*=\s*(['\"])(.*?)\1", tag, flags=re.I | re.S)
        if not name or name.group(2).strip().casefold() != "build":
            continue
        content = re.search(r"\bcontent\s*=\s*(['\"])(.*?)\1", tag, flags=re.I | re.S)
        if not content:
            continue
        match = re.search(r"Build\s+Version\s*:\s*([^\s<]+)", content.group(2), flags=re.I)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return ""


def _stop_requested(stop_event: Any | None) -> bool:
    try:
        return bool(stop_event is not None and stop_event.is_set())
    except Exception:
        return False


def _raise_if_stopped(stop_event: Any | None) -> None:
    if _stop_requested(stop_event):
        raise InterruptedError("Detención solicitada durante auth Evolution de Red Tiger.")


def _wait_interruptible(page: Any, delay_ms: int, stop_event: Any | None) -> None:
    remaining = max(0, int(delay_ms))
    while remaining > 0:
        _raise_if_stopped(stop_event)
        step = min(100, remaining)
        page.wait_for_timeout(step)
        remaining -= step
    _raise_if_stopped(stop_event)


def _same_origin(candidate: str, origin: str) -> bool:
    left = urlparse(str(candidate or ""))
    right = urlparse(str(origin or ""))
    return (
        left.scheme.casefold() == right.scheme.casefold()
        and left.netloc.casefold() == right.netloc.casefold()
        and bool(left.scheme and left.netloc)
    )


def entry_json_url(entry: str, entry_origin: str, client_version: str) -> str:
    """Build the JSON auth request exactly from a live token/demo entry URL."""
    version = str(client_version or "").strip()
    if not version:
        raise ValueError("Red Tiger Evolution auth: client_version vacío.")

    candidate = urljoin(entry_origin.rstrip("/") + "/", str(entry or "").strip())
    if not _same_origin(candidate, entry_origin):
        raise ValueError("Red Tiger Evolution auth: entry fuera del origin anunciado.")

    parsed = urlparse(candidate)
    pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"json", "cc", "client_version"}
    ]
    pairs.extend(
        [
            ("json", "true"),
            ("cc", "1"),
            ("client_version", version),
        ]
    )
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(pairs), parsed.fragment)
    )


def loader_target_url(frontend_url: str, auth_location: str, entry_origin: str) -> str:
    """Apply the auth location to the already validated Evolution loader path."""
    frontend = urlparse(str(frontend_url or ""))
    if not frontend.scheme or not frontend.netloc:
        raise ValueError("Red Tiger Evolution auth: frontend URL inválida.")
    if not _same_origin(frontend_url, entry_origin):
        raise ValueError("Red Tiger Evolution auth: frontend fuera del entry origin.")

    location = urlparse(urljoin(entry_origin.rstrip("/") + "/", str(auth_location or "").strip()))
    if not _same_origin(location.geturl(), entry_origin):
        raise ValueError("Red Tiger Evolution auth: location fuera del entry origin.")

    fragment_pairs = parse_qsl(location.fragment, keep_blank_values=True)
    if not any(key == "origin" for key, _value in fragment_pairs):
        parsed_origin = urlparse(entry_origin)
        bare_origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}"
        fragment_pairs.append(("origin", bare_origin))

    return urlunparse(
        (
            frontend.scheme,
            frontend.netloc,
            frontend.path,
            frontend.params,
            location.query,
            urlencode(fragment_pairs),
        )
    )


def _query_keys(value: str) -> list[str]:
    parsed = urlparse(str(value or ""))
    return sorted({key for key, _value in parse_qsl(parsed.query, keep_blank_values=True) if key})


def _fragment_keys(value: str) -> list[str]:
    parsed = urlparse(str(value or ""))
    return sorted({key for key, _value in parse_qsl(parsed.fragment, keep_blank_values=True) if key})


def _abort_browser_fetch(page: Any) -> None:
    try:
        page.evaluate(
            """() => {
                const state = window.__testerSpinRedTigerAuth;
                if (state && state.controller) state.controller.abort();
            }"""
        )
    except Exception:
        pass


def _browser_fetch_json(
    page: Any,
    url: str,
    timeout_ms: int,
    stop_event: Any | None = None,
) -> tuple[int, str]:
    """Issue auth in browser JS while remaining cooperatively cancellable."""
    bounded_timeout_ms = max(2_000, min(8_000, int(timeout_ms)))
    _raise_if_stopped(stop_event)

    page.evaluate(
        """({url}) => {
            const controller = new AbortController();
            const state = {
                done: false,
                status: 0,
                text: '',
                error: '',
                controller
            };
            window.__testerSpinRedTigerAuth = state;
            fetch(url, {
                method: 'GET',
                credentials: 'include',
                cache: 'no-store',
                headers: {Accept: 'application/json, text/plain, */*'},
                signal: controller.signal
            }).then(async response => {
                state.status = response.status;
                state.text = await response.text();
            }).catch(error => {
                state.error = String(error);
            }).finally(() => {
                state.done = true;
            });
        }""",
        {"url": url},
    )

    remaining = bounded_timeout_ms
    while remaining > 0:
        if _stop_requested(stop_event):
            _abort_browser_fetch(page)
            raise InterruptedError("Detención solicitada durante fetch Evolution de Red Tiger.")
        state = page.evaluate(
            """() => {
                const s = window.__testerSpinRedTigerAuth;
                if (!s) return null;
                return {done: !!s.done, status: s.status || 0, text: s.text || '', error: s.error || ''};
            }"""
        )
        if isinstance(state, dict) and state.get("done"):
            status = int(state.get("status") or 0)
            text = str(state.get("text") or "")
            error = str(state.get("error") or "")
            try:
                page.evaluate("() => { delete window.__testerSpinRedTigerAuth; }")
            except Exception:
                pass
            if status <= 0:
                raise RuntimeError(
                    "Red Tiger Evolution auth JSON: fetch del navegador falló antes de recibir HTTP "
                    f"({error[:180] or 'sin detalle'})."
                )
            return status, text
        step = min(100, remaining)
        page.wait_for_timeout(step)
        remaining -= step

    _abort_browser_fetch(page)
    raise RuntimeError(
        f"Red Tiger Evolution auth JSON: fetch del navegador agotó {bounded_timeout_ms} ms."
    )


def resolve_json_entry_auth(
    context: Any,
    *,
    entry: str,
    entry_origin: str,
    referer: str,
    timeout_ms: int,
    stop_event: Any | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolve Evolution's JSON entry contract in the current browser context."""
    frontend_url = ""
    version = ""
    frontend_statuses: list[dict[str, Any]] = []
    auth_page = None
    step_timeout_ms = max(3_000, min(5_000, int(timeout_ms)))

    try:
        for path in _FRONTEND_PATHS:
            _raise_if_stopped(stop_event)
            candidate = urljoin(entry_origin.rstrip("/") + "/", path.lstrip("/"))
            auth_page = context.new_page()
            try:
                response = auth_page.goto(
                    candidate,
                    wait_until="commit",
                    timeout=step_timeout_ms,
                    referer=referer or None,
                )
                status = int(response.status) if response is not None else 0
            except Exception:
                status = 0
            _raise_if_stopped(stop_event)
            frontend_statuses.append({"path": urlparse(candidate).path, "status": status})
            if status >= 400 or status <= 0:
                auth_page.close()
                auth_page = None
                continue

            # The build meta is in the server HTML; poll briefly instead of waiting
            # for every frontend resource/DOMContentLoaded event.
            for _ in range(25):
                _raise_if_stopped(stop_event)
                try:
                    found = client_version_from_html(auth_page.content())
                except Exception:
                    found = ""
                if found:
                    frontend_url = candidate
                    version = found
                    break
                _wait_interruptible(auth_page, 100, stop_event)
            if frontend_url and version:
                break
            auth_page.close()
            auth_page = None

        if not frontend_url or not version or auth_page is None:
            raise RuntimeError(
                "Red Tiger Evolution auth: no se pudo descubrir client_version desde el frontend oficial; "
                f"candidatos={frontend_statuses!r}."
            )

        _wait_interruptible(auth_page, 250, stop_event)
        auth_url = entry_json_url(entry, entry_origin, version)
        auth_statuses: list[int] = []
        auth_text = ""
        for attempt in range(2):
            _raise_if_stopped(stop_event)
            auth_status, auth_text = _browser_fetch_json(
                auth_page,
                auth_url,
                step_timeout_ms,
                stop_event=stop_event,
            )
            auth_statuses.append(auth_status)
            if auth_status < 400:
                break
            if auth_status not in {401, 403} or attempt:
                break
            _wait_interruptible(auth_page, 350, stop_event)

        auth_status = auth_statuses[-1]
        if auth_status >= 400:
            raise RuntimeError(
                "Red Tiger Evolution auth JSON rechazado desde fetch del navegador: "
                f"HTTP {auth_status}; intentos={auth_statuses!r}; query={_query_keys(auth_url)!r}."
            )

        try:
            payload = json.loads(auth_text)
        except Exception as exc:
            raise RuntimeError("Red Tiger Evolution auth JSON: respuesta no JSON.") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Red Tiger Evolution auth JSON: respuesta no es objeto.")
        location = str(payload.get("location") or "").strip()
        if not location:
            raise RuntimeError("Red Tiger Evolution auth JSON: falta location.")

        target = loader_target_url(frontend_url, location, entry_origin)
        diagnostics = {
            "frontend_path": urlparse(frontend_url).path,
            "client_version": version,
            "frontend_statuses": frontend_statuses,
            "auth_status": auth_status,
            "auth_statuses": auth_statuses,
            "auth_transport": "browser-fetch",
            "auth_query_keys": _query_keys(auth_url),
            "target_query_keys": _query_keys(target),
            "target_fragment_keys": _fragment_keys(target),
        }
        return target, diagnostics
    finally:
        if auth_page is not None:
            try:
                _abort_browser_fetch(auth_page)
                auth_page.close()
            except Exception:
                pass
