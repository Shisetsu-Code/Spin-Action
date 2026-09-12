from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tester_spin.providers.redtiger import bootstrap_browser as _browser_bootstrap
from tester_spin.providers.redtiger.bootstrap import BootstrapEndpoints
from tester_spin.providers.redtiger.runtime import RedTigerRuntime


def _stop_requested(stop_event: Any | None) -> bool:
    try:
        return bool(stop_event is not None and stop_event.is_set())
    except Exception:
        return False


def _raise_if_stopped(stop_event: Any | None) -> None:
    if _stop_requested(stop_event):
        raise InterruptedError("Detención solicitada durante start de Evolution Games.")


def _abort_start_fetch(page: Any) -> None:
    try:
        page.evaluate(
            """() => {
                const state = window.__testerSpinEvolutionStart;
                if (state && state.controller) state.controller.abort();
                delete window.__testerSpinEvolutionStart;
            }"""
        )
    except Exception:
        pass


def _wait_for_live_start_contract(
    page: Any,
    *,
    expected_launch_id: str,
    timeout_ms: int,
    stop_event: Any | None,
) -> str:
    """Wait only for the live data required by the captured Evolution start call.

    The public page contains the authoritative WordPress post id in
    ``.game-playable[data-game-id]`` and emits two live globals before the external
    game loader script: ``evo_casino_config.api_url`` and
    ``game_api_settings.nonce``. The HAR proves that those values are sufficient
    for ``GET /wp-json/games/v1/start``. Requiring the external
    ``EvolutionGameLoader`` constructor made bootstrap depend on an unrelated
    static JS asset finishing successfully.
    """
    deadline = time.monotonic() + min(10.0, max(2.0, float(timeout_ms) / 1000.0))
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _raise_if_stopped(stop_event)
        try:
            state = page.evaluate(
                """() => {
                    const root = document.querySelector('.game-playable');
                    const form = document.querySelector('form.start-form');
                    const button = document.querySelector('#start-game');
                    const api = window.evo_casino_config && window.evo_casino_config.api_url;
                    const nonce = window.game_api_settings && window.game_api_settings.nonce;
                    let apiHost = '';
                    let apiPath = '';
                    try {
                        if (api) {
                            const parsed = new URL(String(api), window.location.href);
                            apiHost = parsed.host;
                            apiPath = parsed.pathname;
                        }
                    } catch (_) {}
                    return {
                        id: root && root.dataset ? String(root.dataset.gameId || '') : '',
                        form: !!form,
                        button: !!button,
                        api_host: apiHost,
                        api_path: apiPath,
                        has_nonce: typeof nonce === 'string' && nonce.length > 0,
                        loader: typeof window.EvolutionGameLoader === 'function'
                    };
                }"""
            )
        except Exception:
            state = None
        if isinstance(state, dict):
            last = dict(state)
            live_id = str(state.get("id") or "").strip()
            if live_id and live_id != expected_launch_id:
                raise ValueError(
                    f"Evolution Games: post id del DOM={live_id!r} != catálogo={expected_launch_id!r}."
                )
            if (
                live_id == expected_launch_id
                and bool(state.get("form"))
                and bool(state.get("button"))
                and str(state.get("api_host") or "").casefold() == "games.evolution.com"
                and str(state.get("api_path") or "").rstrip("/") == "/wp-json"
                and bool(state.get("has_nonce"))
            ):
                return live_id
        page.wait_for_timeout(100)

    safe_last = {
        "id": str(last.get("id") or ""),
        "form": bool(last.get("form")),
        "button": bool(last.get("button")),
        "api_host": str(last.get("api_host") or ""),
        "api_path": str(last.get("api_path") or ""),
        "has_nonce": bool(last.get("has_nonce")),
        "loader": bool(last.get("loader")),
    }
    raise TimeoutError(
        "Evolution Games: la página no expuso el contrato vivo de start "
        f"para post id={expected_launch_id!r}; estado={safe_last!r}."
    )


def _submit_live_start(page: Any, *, stop_event: Any | None) -> None:
    """Reproduce the captured start request using only values from the live page.

    No nonce, endpoint credential, game id or launch payload from the HAR is
    stored. The current page supplies its own nonce/API base and the response
    supplies a fresh showcase URL. The fresh payload is installed as an iframe,
    matching the site's game loader, without exposing the opaque URL to Python.
    """
    _raise_if_stopped(stop_event)
    launch = page.evaluate(
        """() => {
            const root = document.querySelector('.game-playable');
            const id = root && root.dataset ? String(root.dataset.gameId || '') : '';
            const api = window.evo_casino_config && window.evo_casino_config.api_url;
            const nonce = window.game_api_settings && window.game_api_settings.nonce;
            if (!root || !id) throw new Error('Evolution start: game id missing from DOM');
            if (typeof api !== 'string' || !api) throw new Error('Evolution start: api_url missing');
            if (typeof nonce !== 'string' || !nonce) throw new Error('Evolution start: live nonce missing');

            const url = new URL('games/v1/start', api.endsWith('/') ? api : api + '/');
            url.searchParams.set('id', id);
            url.searchParams.set('mobile', 'false');
            if (url.host !== window.location.host || url.pathname !== '/wp-json/games/v1/start') {
                throw new Error('Evolution start: unexpected live API target');
            }

            const controller = new AbortController();
            const state = {
                done: false,
                status: 0,
                success: false,
                error: '',
                payload_host: '',
                payload_path: '',
                controller
            };
            window.__testerSpinEvolutionStart = state;

            fetch(url.href, {
                method: 'GET',
                credentials: 'same-origin',
                headers: {'X-WP-Nonce': nonce},
                signal: controller.signal
            }).then(async response => {
                state.status = response.status;
                const text = await response.text();
                if (!response.ok) {
                    state.error = 'HTTP ' + response.status;
                    return;
                }
                let data;
                try {
                    data = JSON.parse(text);
                } catch (_) {
                    state.error = 'JSON inválido';
                    return;
                }
                if (!data || data.success !== true || typeof data.payload !== 'string' || !data.payload) {
                    state.error = data && data.error ? String(data.error).slice(0, 180) : 'respuesta start sin payload';
                    return;
                }
                let target;
                try {
                    target = new URL(data.payload);
                } catch (_) {
                    state.error = 'payload URL inválida';
                    return;
                }
                if (!target.host.endsWith('.evo-games.com') && target.host !== 'evo-games.com') {
                    state.error = 'payload host inesperado';
                    return;
                }
                state.payload_host = target.host;
                state.payload_path = target.pathname;

                const iframe = document.createElement('iframe');
                iframe.src = data.payload;
                iframe.height = '100%';
                iframe.width = '100%';
                iframe.setAttribute('allow', 'fullscreen');
                iframe.style.cssText = 'position:absolute;top:0;left:0;border:0;';
                root.innerHTML = '';
                root.appendChild(iframe);
                state.success = true;
            }).catch(error => {
                state.error = String(error).slice(0, 180);
            }).finally(() => {
                state.done = true;
            });

            return {
                id,
                api_path: url.pathname,
                query_keys: Array.from(url.searchParams.keys()).sort()
            };
        }"""
    )
    if not isinstance(launch, dict) or str(launch.get("id") or "").strip() == "":
        _abort_start_fetch(page)
        raise RuntimeError("Evolution Games: no se pudo iniciar el request start desde la página viva.")

    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if _stop_requested(stop_event):
            _abort_start_fetch(page)
            raise InterruptedError("Detención solicitada durante /games/v1/start.")
        try:
            state = page.evaluate(
                """() => {
                    const s = window.__testerSpinEvolutionStart;
                    if (!s) return null;
                    return {
                        done: !!s.done,
                        status: Number(s.status || 0),
                        success: !!s.success,
                        error: String(s.error || ''),
                        payload_host: String(s.payload_host || ''),
                        payload_path: String(s.payload_path || '')
                    };
                }"""
            )
        except Exception:
            state = None
        if isinstance(state, dict) and state.get("done"):
            try:
                page.evaluate("() => { delete window.__testerSpinEvolutionStart; }")
            except Exception:
                pass
            if not state.get("success"):
                raise RuntimeError(
                    "Evolution Games: /games/v1/start no produjo launcher válido; "
                    f"HTTP={int(state.get('status') or 0)}, error={str(state.get('error') or '')!r}."
                )
            return
        page.wait_for_timeout(100)

    _abort_start_fetch(page)
    raise TimeoutError("Evolution Games: /games/v1/start no terminó en 8000 ms.")


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
    """Use the existing Red Tiger bootstrap with the HAR-grounded Evolution start step."""
    old_wait = _browser_bootstrap._wait_for_official_loader
    old_submit = _browser_bootstrap._submit_official_start
    _browser_bootstrap._wait_for_official_loader = _wait_for_live_start_contract
    _browser_bootstrap._submit_official_start = _submit_live_start
    try:
        return _browser_bootstrap.bootstrap_game(
            public_url,
            table_id,
            timeout_s=timeout_s,
            artifact_dir=artifact_dir,
            endpoints=endpoints,
            progress=progress,
            stop_event=stop_event,
        )
    finally:
        _browser_bootstrap._wait_for_official_loader = old_wait
        _browser_bootstrap._submit_official_start = old_submit
