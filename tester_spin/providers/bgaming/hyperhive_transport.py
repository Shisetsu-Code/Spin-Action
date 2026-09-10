from __future__ import annotations

import threading
from typing import Any
from urllib.parse import quote, urlparse

from tester_spin.providers.bgaming.runtime import extract_script_urls


_install_lock = threading.Lock()
_installed = False
_hydrated_runtime_ids: set[int] = set()


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


def prepare_hyperhive_client(
    runtime: Any,
    *,
    timeout_s: float,
    force: bool = False,
) -> str:
    """Load the live inner client and merge its referenced scripts into runtime.

    This is deliberately runtime-only discovery. No HAR data is consulted.
    A runtime is marked hydrated only after the iframe GET succeeds, so a
    transient failure can be retried later.
    """
    client_url = hyperhive_client_url(runtime)
    outer_url = str(getattr(runtime, "launch_url", "") or "")
    runtime_id = id(runtime)

    if not client_url or client_url == outer_url:
        return client_url
    if runtime_id in _hydrated_runtime_ids and not force:
        return client_url

    response = runtime.session.get(
        client_url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": outer_url,
        },
        timeout=timeout_s,
    )
    response.raise_for_status()

    discovered = extract_script_urls(response.text, response.url or client_url)
    existing = list(getattr(runtime, "script_urls", None) or [])
    seen = set(existing)
    for url in discovered:
        if url in seen:
            continue
        existing.append(url)
        seen.add(url)
    runtime.script_urls = existing

    _hydrated_runtime_ids.add(runtime_id)
    return response.url or client_url


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
