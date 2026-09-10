from __future__ import annotations

import re
import threading
import weakref
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from tester_spin.providers.bgaming.runtime import (
    _provider_script_url,
    extract_script_urls,
)


_install_lock = threading.Lock()
_installed = False
# Cache by the actual live HTTP session, not id(runtime). Python may reuse object
# ids after GC, which could incorrectly skip iframe discovery for a later game.
_hydrated_clients: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()


def hyperhive_client_url(runtime: Any) -> str:
    """Return the real inner HyperHive client URL for the current live session.

    The outer /hyperhive launch page is only a container. It exposes the fresh
    play_token in window.__OPTIONS__ and loads the actual game client in an
    iframe at /?token=.... Contract discovery must inspect that inner document,
    because that is where the game-specific scripts that build JSON-RPC play
    requests are referenced.
    """
    launch_url = str(getattr(runtime, "launch_url", "") or "")
    parsed = urlparse(launch_url)
    if parsed.path.rstrip("/").casefold() != "/hyperhive":
        return launch_url

    options = getattr(runtime, "options", None)
    play_token = (
        str(options.get("play_token") or "").strip()
        if isinstance(options, dict)
        else ""
    )
    if not play_token or not parsed.scheme or not parsed.netloc:
        return launch_url

    origin = f"{parsed.scheme}://{parsed.netloc}"
    return origin + "/?token=" + quote(play_token, safe="")


def _literal_assignment(text: str, name: str) -> str:
    """Read a small literal JS string assignment without evaluating JavaScript."""
    match = re.search(
        rf"\b(?:var|let|const)?\s*{re.escape(name)}\s*=\s*['\"]([^'\"]{{0,160}})['\"]",
        text or "",
    )
    return str(match.group(1)) if match else ""


def _dynamic_loader_script_urls(runtime: Any, html: str, base_url: str) -> list[str]:
    """Recover scripts referenced by BGaming's inline loadScript bootstrap.

    HyperHive inner pages commonly create script elements dynamically instead
    of exposing client/game scripts as <script src>. The loader still contains
    literal provider paths, sometimes in template strings such as
    ./game${versionPath}/gamesFilesHashes.js. Resolve only literal substitutions
    proven in the same HTML; never execute the page JavaScript.
    """
    substitutions = {
        "versionPath": _literal_assignment(html, "versionPath"),
        "gamePath": _literal_assignment(html, "gamePath"),
    }
    out: list[str] = []
    seen: set[str] = set()

    for match in re.finditer(r"(['\"`])([^'\"`]{1,400}\.js)\1", html or ""):
        raw = str(match.group(2) or "").strip()
        if not raw:
            continue
        unresolved = False
        for variable, literal in substitutions.items():
            marker = "${" + variable + "}"
            if marker in raw:
                if literal == "" and not re.search(
                    rf"\b(?:var|let|const)?\s*{re.escape(variable)}\s*=\s*['\"]['\"]",
                    html or "",
                ):
                    unresolved = True
                    break
                raw = raw.replace(marker, literal)
        if unresolved or "${" in raw:
            continue
        url = urljoin(base_url, raw)
        if not _provider_script_url(runtime, url) or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _hash_manifest_script_urls(runtime: Any, manifest_url: str, text: str) -> list[str]:
    """Convert BGaming *FilesHashes.js rows into the exact keyed JS URLs.

    The browser requests game binaries with ?key=<hash>. Some demo endpoints do
    not reliably serve an unkeyed fallback, so contract discovery must reproduce
    the same keyed URLs advertised by the live manifest.
    """
    out: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(
        r'["\']fileName["\']\s*:\s*["\']([^"\']+\.js)["\']\s*,\s*'
        r'["\']hash["\']\s*:\s*["\']([A-Fa-f0-9]{8,128})["\']',
        text or "",
    ):
        file_name = str(match.group(1) or "").strip()
        digest = str(match.group(2) or "").strip()
        if not file_name or not digest:
            continue
        base = urljoin(manifest_url, file_name)
        url = base + ("&" if "?" in base else "?") + "key=" + digest
        if not _provider_script_url(runtime, url) or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def prepare_hyperhive_client(
    runtime: Any,
    *,
    timeout_s: float,
    force: bool = False,
) -> str:
    """Load the live inner client and merge all proven client scripts into runtime.

    Discovery covers both ordinary <script src> references and BGaming's inline
    dynamic loader plus its live hash manifests. No HAR data is consulted at
    runtime. Hydration is cached by the live requests.Session plus client URL. A
    failed iframe GET is never cached, so transient failures remain retryable.
    """
    client_url = hyperhive_client_url(runtime)
    outer_url = str(getattr(runtime, "launch_url", "") or "")
    session = getattr(runtime, "session", None)

    if not client_url or client_url == outer_url:
        return client_url
    if session is not None and not force:
        try:
            if _hydrated_clients.get(session) == client_url:
                return client_url
        except TypeError:
            # Unexpected non-weakrefable session-like objects simply skip cache.
            pass

    response = runtime.session.get(
        client_url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": outer_url,
        },
        timeout=timeout_s,
    )
    response.raise_for_status()

    response_url = str(getattr(response, "url", "") or client_url)
    discovered = extract_script_urls(response.text, response_url)
    discovered.extend(
        _dynamic_loader_script_urls(runtime, response.text or "", response_url)
    )

    # The dynamic bootstrap first fetches hash manifests and then composes the
    # actual JS URLs. Resolve those manifests here so engine discovery sees the
    # exact client.min.js/game/*.min.js resources the browser sees.
    keyed_scripts: list[str] = []
    for manifest_url in list(dict.fromkeys(discovered)):
        if "fileshashes.js" not in urlparse(manifest_url).path.casefold():
            continue
        try:
            manifest_response = runtime.session.get(manifest_url, timeout=timeout_s)
            manifest_response.raise_for_status()
        except Exception:
            continue
        keyed_scripts.extend(
            _hash_manifest_script_urls(
                runtime,
                str(getattr(manifest_response, "url", "") or manifest_url),
                manifest_response.text or "",
            )
        )
    discovered.extend(keyed_scripts)

    existing = list(getattr(runtime, "script_urls", None) or [])
    seen = set(existing)
    for url in discovered:
        if url in seen:
            continue
        existing.append(url)
        seen.add(url)
    runtime.script_urls = existing

    if session is not None:
        try:
            _hydrated_clients[session] = client_url
        except TypeError:
            pass
    return response_url


def _hydrate_inner_client(runtime: Any, client_url: str, timeout_s: float) -> None:
    """Best-effort iframe hydration for callers that reached RPC directly."""
    if not client_url:
        return
    try:
        prepare_hyperhive_client(runtime, timeout_s=timeout_s)
    except Exception:
        # RPC may still be useful for diagnostics; importantly, a failed GET is
        # not cached as hydrated, so a later attempt can retry it.
        return


def install_hyperhive_transport_adapter() -> None:
    """Wrap the HyperHive RPC path with the live inner-frame request context."""
    global _installed
    with _install_lock:
        if _installed:
            return

        from tester_spin.providers.bgaming import hyperhive

        original_rpc = hyperhive._rpc

        def contextual_rpc(
            runtime,
            method: str,
            *,
            timeout_s: float,
            params: dict[str, Any],
            rpc_id: int | str | None = None,
        ):
            outer_url = str(runtime.launch_url)
            client_url = hyperhive_client_url(runtime)
            if client_url == outer_url:
                return original_rpc(
                    runtime,
                    method,
                    timeout_s=timeout_s,
                    params=params,
                    rpc_id=rpc_id,
                )

            if method == "init":
                _hydrate_inner_client(runtime, client_url, timeout_s)

            # hyperhive._rpc derives both Origin and Referer from launch_url.
            # Temporarily exposing the live inner iframe URL reproduces the
            # browser request context while keeping the canonical outer launch.
            runtime.launch_url = client_url
            try:
                return original_rpc(
                    runtime,
                    method,
                    timeout_s=timeout_s,
                    params=params,
                    rpc_id=rpc_id,
                )
            finally:
                runtime.launch_url = outer_url

        hyperhive._rpc = contextual_rpc
        _installed = True


__all__ = [
    "hyperhive_client_url",
    "prepare_hyperhive_client",
    "install_hyperhive_transport_adapter",
]
