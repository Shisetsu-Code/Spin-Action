from __future__ import annotations

import re
import threading
import weakref
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from tester_spin.providers.bgaming.runtime import (
    _provider_script_references,
    _provider_script_url,
    extract_script_urls,
    sanitize_error_text,
    sanitize_session_url,
)


_install_lock = threading.Lock()
_installed = False
# Cache by the actual live HTTP session, not id(runtime). Python may reuse object
# ids after GC, which could incorrectly skip iframe discovery for a later game.
_hydrated_clients: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()

# These are provider-level script roles advertised by HyperHive's own hash
# manifests. A role script is contract evidence even when it does not itself
# contain the transport words jsonrpc/state_lock. In particular,
# integration.min.js can contain only feature-buy selector logic such as
# isNormalBuy/isSuperBuy while client.min.js owns the JSON-RPC serializer.
_ENGINE_CONTRACT_BASENAMES = {
    "client.min.js",
    "common.min.js",
    "game.min.js",
    "integration.min.js",
}


def _transport_diagnostics(runtime: Any) -> list[dict[str, Any]]:
    options = getattr(runtime, "options", None)
    if not isinstance(options, dict):
        return []
    rows = options.get("_hyperhive_transport_diagnostics")
    if not isinstance(rows, list):
        rows = []
        options["_hyperhive_transport_diagnostics"] = rows
    return rows


def _record_transport_diagnostic(runtime: Any, kind: str, **fields: Any) -> None:
    rows = _transport_diagnostics(runtime)
    safe: dict[str, Any] = {"kind": str(kind)}
    for key, value in fields.items():
        if key.endswith("_url"):
            safe[key] = sanitize_session_url(str(value or ""))
        elif key.endswith("_urls") and isinstance(value, (list, tuple)):
            safe[key] = [
                sanitize_session_url(str(item or ""))
                for item in value
                if str(item or "")
            ][:32]
        elif key == "error":
            safe[key] = sanitize_error_text(str(value or ""))[:1200]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
    rows.append(safe)
    if len(rows) > 64:
        del rows[:-64]



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


def _loader_res_value(text: str) -> str:
    """Recover a BGaming loader resource version without evaluating JavaScript."""
    direct = _literal_assignment(text, "res")
    if direct:
        return direct
    match = re.search(
        r"\\bres\\s*(?::|=)[^\\\"\']*[\\\"\']([^\\\"\']+?)[\\\"\']",
        text or "",
    )
    return str(match.group(1) or "") if match else ""

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

    # Some HyperHive loaders expose a version/resource directory as a literal
    # "res" field and then append bundle.js at runtime. This is provider-owned
    # path evidence, not a title rule. Follow only a bounded relative res value.
    res = _loader_res_value(html).strip().strip("/")
    if (
        res
        and "bundle.js" in (html or "")
        and "://" not in res
        and ".." not in res.split("/")
    ):
        candidates = [urljoin(base_url, res + "/bundle.js")]
        options = getattr(runtime, "options", None)
        resources_path = (
            str(options.get("resources_path") or "").strip()
            if isinstance(options, dict)
            else ""
        )
        if resources_path:
            candidates.append(
                urljoin(resources_path.rstrip("/") + "/", res + "/bundle.js")
            )
        for url in candidates:
            if not _provider_script_url(runtime, url) or url in seen:
                continue
            seen.add(url)
            out.append(url)
    return out


def _expand_provider_script_graph(
    runtime: Any,
    seeds: list[str],
    *,
    timeout_s: float,
    max_depth: int = 2,
    max_scripts: int = 24,
) -> list[str]:
    """Follow provider-owned JS loader references without executing JavaScript."""
    queue: list[tuple[str, int]] = [
        (url, 0) for url in seeds if _provider_script_url(runtime, url)
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    while queue and len(seen) < max_scripts:
        url, depth = queue.pop(0)
        if url in seen or not _provider_script_url(runtime, url):
            continue
        seen.add(url)
        ordered.append(url)
        if depth >= max_depth:
            continue
        try:
            response = runtime.session.get(url, timeout=timeout_s)
            response.raise_for_status()
        except Exception as exc:
            _record_transport_diagnostic(
                runtime,
                "script-graph-error",
                script_url=url,
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        response_url = str(getattr(response, "url", "") or url)
        text = response.text or ""
        children = _provider_script_references(
            runtime,
            parent_url=response_url,
            text=text,
        )
        for child in _dynamic_loader_script_urls(
            runtime,
            text,
            response_url,
        ):
            if child not in children:
                children.append(child)
        _record_transport_diagnostic(
            runtime,
            "script-graph-node",
            script_url=response_url,
            status=int(getattr(response, "status_code", 0) or 0),
            bytes=len(text.encode("utf-8", errors="replace")),
            child_script_urls=children,
        )
        for child in children:
            if child not in seen:
                queue.append((child, depth + 1))
    return ordered

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

    _record_transport_diagnostic(
        runtime,
        "inner-client-request",
        client_url=client_url,
        play_token_present=bool(
            isinstance(getattr(runtime, "options", None), dict)
            and runtime.options.get("play_token")
        ),
    )
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
    except Exception as exc:
        _record_transport_diagnostic(
            runtime,
            "inner-client-error",
            client_url=client_url,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise

    response_url = str(getattr(response, "url", "") or client_url)
    static_scripts = extract_script_urls(response.text, response_url)
    dynamic_scripts = _dynamic_loader_script_urls(
        runtime,
        response.text or "",
        response_url,
    )
    discovered = [*static_scripts, *dynamic_scripts]
    provider_seeds = [
        url for url in discovered if _provider_script_url(runtime, url)
    ]
    options = getattr(runtime, "options", None)
    if isinstance(options, dict):
        for key in ("games_loader_source", "game_bundle_source"):
            configured = str(options.get(key) or "").strip()
            if (
                configured
                and _provider_script_url(runtime, configured)
                and configured not in provider_seeds
            ):
                provider_seeds.append(configured)
    expanded_scripts = _expand_provider_script_graph(
        runtime,
        provider_seeds,
        timeout_s=timeout_s,
    )
    for url in expanded_scripts:
        if url not in discovered:
            discovered.append(url)

    _record_transport_diagnostic(
        runtime,
        "inner-client-response",
        client_url=client_url,
        response_url=response_url,
        status=int(getattr(response, "status_code", 0) or 0),
        html_bytes=len((response.text or "").encode("utf-8", errors="replace")),
        static_scripts=len(static_scripts),
        dynamic_scripts=len(dynamic_scripts),
        static_script_urls=static_scripts,
        dynamic_script_urls=dynamic_scripts,
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
        except Exception as exc:
            _record_transport_diagnostic(
                runtime,
                "manifest-error",
                manifest_url=manifest_url,
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        resolved_manifest_url = str(
            getattr(manifest_response, "url", "") or manifest_url
        )
        manifest_scripts = _hash_manifest_script_urls(
            runtime,
            resolved_manifest_url,
            manifest_response.text or "",
        )
        keyed_scripts.extend(manifest_scripts)
        _record_transport_diagnostic(
            runtime,
            "manifest-response",
            manifest_url=resolved_manifest_url,
            status=int(getattr(manifest_response, "status_code", 0) or 0),
            keyed_scripts=len(manifest_scripts),
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
    _record_transport_diagnostic(
        runtime,
        "inner-client-complete",
        response_url=response_url,
        discovered_scripts=len(discovered),
        keyed_scripts=len(keyed_scripts),
        discovered_script_urls=discovered,
        keyed_script_urls=keyed_scripts,
        total_runtime_scripts=len(existing),
    )

    if session is not None:
        try:
            _hydrated_clients[session] = client_url
        except TypeError:
            pass
    return response_url


def _engine_role_script(url: str) -> bool:
    basename = urlparse(str(url or "")).path.rsplit("/", 1)[-1].casefold()
    return basename in _ENGINE_CONTRACT_BASENAMES


def _append_engine_role_contracts(
    runtime: Any,
    base_contract: str,
    *,
    timeout_s: float,
) -> str:
    """Append live HyperHive role scripts that the generic marker filter dropped.

    The old collector kept a script only when that *individual file* contained a
    transport marker. HyperHive splits responsibilities across files, so a
    feature serializer can be semantically required while containing none of
    those words. Keep exact role files advertised by the live hash manifests and
    merge them before wire analysis. This is provider-generic and never routes by
    game name, slug or identifier.
    """
    texts: list[str] = [base_contract] if base_contract else []
    seen_texts = {base_contract} if base_contract else set()
    seen_urls: set[str] = set()
    added_bytes = 0
    max_extra_bytes = 8 * 1024 * 1024

    for url in list(getattr(runtime, "script_urls", None) or []):
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        if not _provider_script_url(runtime, url) or not _engine_role_script(url):
            continue
        try:
            response = runtime.session.get(url, timeout=timeout_s)
            response.raise_for_status()
            text = response.text or ""
        except Exception:
            continue
        if not text or text in seen_texts:
            continue
        encoded_size = len(text.encode("utf-8", errors="replace"))
        if added_bytes + encoded_size > max_extra_bytes:
            continue
        texts.append(text)
        seen_texts.add(text)
        added_bytes += encoded_size

    return "\n".join(texts)


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
    """Wrap HyperHive discovery/RPC with the live inner-frame request context."""
    global _installed
    with _install_lock:
        if _installed:
            return

        from tester_spin.providers.bgaming import hyperhive

        original_rpc = hyperhive._rpc
        original_download_engine_contract = hyperhive._download_engine_contract

        def complete_engine_contract(
            runtime,
            *,
            timeout_s: float,
        ) -> str:
            # Hydrate first so runtime.script_urls contains the exact keyed role
            # scripts advertised by the inner page's manifests.
            try:
                prepare_hyperhive_client(runtime, timeout_s=timeout_s)
            except Exception:
                pass
            base_contract = original_download_engine_contract(
                runtime,
                timeout_s=timeout_s,
            )
            return _append_engine_role_contracts(
                runtime,
                base_contract,
                timeout_s=timeout_s,
            )

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

        hyperhive._download_engine_contract = complete_engine_contract
        hyperhive._rpc = contextual_rpc
        _installed = True


__all__ = [
    "hyperhive_client_url",
    "prepare_hyperhive_client",
    "install_hyperhive_transport_adapter",
]
