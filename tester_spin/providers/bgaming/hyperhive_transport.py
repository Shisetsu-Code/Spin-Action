from __future__ import annotations

import threading
from typing import Any
from urllib.parse import quote, urlparse


_install_lock = threading.Lock()
_installed = False
_hydrated_runtime_ids: set[int] = set()


def hyperhive_client_url(runtime: Any) -> str:
    """Return the actual inner HyperHive client URL demonstrated by browser HARs.

    BGaming's outer /hyperhive launch page is a container.  It exposes play_token
    in window.__OPTIONS__ and loads the actual game in an iframe at /?token=....
    JSON-RPC requests originate from that iframe, so its URL is the correct
    Referer for /api rather than the outer launch_token URL.
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


def _hydrate_inner_client(runtime: Any, client_url: str, timeout_s: float) -> None:
    """Best-effort reproduction of the iframe GET seen before JSON-RPC init."""
    runtime_id = id(runtime)
    if runtime_id in _hydrated_runtime_ids:
        return

    outer_url = str(getattr(runtime, "launch_url", "") or "")
    if not client_url or client_url == outer_url:
        return

    try:
        response = runtime.session.get(
            client_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": outer_url,
            },
            timeout=timeout_s,
        )
        response.raise_for_status()
    except Exception:
        # The GET is context hydration, not protocol authority.  RPC still runs
        # with the HAR-proven Referer even if a transient demo GET fails.
        pass
    finally:
        _hydrated_runtime_ids.add(runtime_id)


def install_hyperhive_transport_adapter() -> None:
    """Wrap the already-installed HyperHive RPC adapter with browser context."""
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
            # The origin is unchanged; temporarily exposing the inner iframe URL
            # makes the generated Referer match the observed browser request.
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
    "install_hyperhive_transport_adapter",
]
