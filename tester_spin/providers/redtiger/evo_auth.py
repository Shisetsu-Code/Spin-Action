from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse


# This is an observed Evolution wrapper family path, not a game identifier. The
# build/version itself is always read from the live HTML before auth is replayed.
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


def _same_origin(candidate: str, origin: str) -> bool:
    left = urlparse(str(candidate or ""))
    right = urlparse(str(origin or ""))
    return (
        left.scheme.casefold() == right.scheme.casefold()
        and left.netloc.casefold() == right.netloc.casefold()
        and bool(left.scheme and left.netloc)
    )


def entry_json_url(entry: str, entry_origin: str, client_version: str) -> str:
    """Build the JSON auth request exactly from a live token/demo entry URL.

    JSESSIONID/params and all opaque values stay untouched. Only the three fields
    proven by the Evolution client contract are added/replaced.
    """
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
    """Apply the auth location to the already validated Evolution loader path.

    This mirrors the captured client: retain the loader pathname, take search/hash
    from the auth response, and expose only the bare remote origin to the loader.
    """
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


def _browser_fetch_json(page: Any, url: str) -> tuple[int, str]:
    """Issue auth from the real Evolution page, not Playwright's API client.

    The captured Evolution bundle performs this request from browser JavaScript.
    BrowserContext.request shares cookies but is still a separate HTTP stack and can
    be rejected by edge protection. Running fetch() in the page preserves the live
    browser fingerprint, cookie jar, referrer/origin semantics and HTTP/2 handling.
    """
    result = page.evaluate(
        """async ({url}) => {
            try {
                const response = await fetch(url, {
                    method: 'GET',
                    credentials: 'include',
                    cache: 'no-store',
                    headers: {Accept: 'application/json, text/plain, */*'}
                });
                return {status: response.status, text: await response.text()};
            } catch (error) {
                return {status: 0, text: '', error: String(error)};
            }
        }""",
        {"url": url},
    )
    if not isinstance(result, dict):
        raise RuntimeError("Red Tiger Evolution auth JSON: fetch del navegador no devolvió resultado.")
    status = int(result.get("status") or 0)
    if status <= 0:
        raise RuntimeError(
            "Red Tiger Evolution auth JSON: fetch del navegador falló antes de recibir HTTP."
        )
    return status, str(result.get("text") or "")


def resolve_json_entry_auth(
    context: Any,
    *,
    entry: str,
    entry_origin: str,
    referer: str,
    timeout_ms: int,
) -> tuple[str, dict[str, Any]]:
    """Resolve Evolution's JSON entry contract in the current browser context.

    The live Evolution bundle performs the auth request from page JavaScript. We do
    the same: load its current frontend in a real page, discover the current build,
    then call the opaque token/demo entry URL through browser fetch(). No session
    credential is decoded, persisted or reconstructed.
    """
    frontend_url = ""
    version = ""
    frontend_statuses: list[dict[str, Any]] = []
    auth_page = None

    try:
        for path in _FRONTEND_PATHS:
            candidate = urljoin(entry_origin.rstrip("/") + "/", path.lstrip("/"))
            auth_page = context.new_page()
            try:
                response = auth_page.goto(
                    candidate,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                    referer=referer or None,
                )
                status = int(response.status) if response is not None else 0
            except Exception:
                status = 0
            frontend_statuses.append({"path": urlparse(candidate).path, "status": status})
            if status >= 400 or status <= 0:
                auth_page.close()
                auth_page = None
                continue

            try:
                found = client_version_from_html(auth_page.content())
            except Exception:
                found = ""
            if found:
                frontend_url = candidate
                version = found
                break
            auth_page.close()
            auth_page = None

        if not frontend_url or not version or auth_page is None:
            raise RuntimeError(
                "Red Tiger Evolution auth: no se pudo descubrir client_version desde el frontend oficial; "
                f"candidatos={frontend_statuses!r}."
            )

        # Give the official frontend/edge scripts a short opportunity to establish
        # any browser-owned cookies before reproducing the same browser-side fetch.
        auth_page.wait_for_timeout(500)
        auth_url = entry_json_url(entry, entry_origin, version)
        auth_statuses: list[int] = []
        auth_text = ""
        for attempt in range(2):
            auth_status, auth_text = _browser_fetch_json(auth_page, auth_url)
            auth_statuses.append(auth_status)
            if auth_status < 400:
                break
            if auth_status not in {401, 403} or attempt:
                break
            auth_page.wait_for_timeout(1000)

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
                auth_page.close()
            except Exception:
                pass
