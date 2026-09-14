from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import GameCallback, Progress
from tester_spin.providers.rubyplay.adapter import RubyPlayProvider as _RubyPlayProvider
from tester_spin.providers.rubyplay.browser_catalog import RubyPlayBrowserCatalogClient


INDEX_BRANCH_ACTIONS = {"select", "pick"}
_GAME_PATH_RE = re.compile(r"^/games/([a-z0-9][a-z0-9-]*)/?$")


class _BrowserHTTPResponse:
    def __init__(
        self,
        *,
        status_code: int,
        url: str,
        text: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = int(status_code)
        self.url = str(url)
        self.text = str(text)
        self._payload = payload

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        response = requests.Response()
        response.status_code = self.status_code
        response.url = self.url
        response._content = self.content
        raise requests.HTTPError(
            f"{self.status_code} error for {self.url}",
            response=response,
        )

    def json(self) -> dict[str, Any]:
        if isinstance(self._payload, dict):
            return self._payload
        parsed = json.loads(self.text)
        if not isinstance(parsed, dict):
            raise ValueError("RubyPlay browser response JSON no es objeto.")
        return parsed


class _BrowserCatalogSession:
    """requests-shaped facade backed by an already verified Chromium page."""

    def __init__(self, browser: RubyPlayBrowserCatalogClient, original: Any) -> None:
        self.browser = browser
        self.original = original
        self.headers = getattr(original, "headers", {})

    def get(self, url: str, **kwargs):
        if str(url).rstrip("/") == self.browser.catalog_url.rstrip("/"):
            html, resolved = self.browser.fetch_catalog_html()
            return _BrowserHTTPResponse(
                status_code=200,
                url=resolved,
                text=html,
            )
        return self.original.get(url, **kwargs)

    def post(
        self,
        url: str,
        *,
        params=None,
        json=None,
        headers=None,
        timeout=None,
        **_kwargs,
    ):
        del timeout
        response = self.browser.request_json(
            url,
            params=dict(params or {}),
            payload=dict(json or {}),
            headers=dict(headers or {}),
        )
        return _BrowserHTTPResponse(
            status_code=response.status,
            url=response.url,
            text=response.body,
            payload=response.data,
        )


def _strict_dom_games(html: str, base_url: str, provider_key: str) -> list[Game]:
    """Extract only canonical RubyPlay /games/<slug>/ targets from rendered DOM."""
    base = urlparse(base_url)
    allowed_host = (base.hostname or "rubyplay.com").lower()
    if allowed_host.startswith("www."):
        allowed_host = allowed_host[4:]

    soup = BeautifulSoup(html or "", "html.parser")
    by_slug: dict[str, Game] = {}
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host != allowed_host:
            continue
        match = _GAME_PATH_RE.fullmatch(parsed.path or "")
        if not match:
            continue
        slug = match.group(1)
        if slug in by_slug:
            continue
        raw_name = " ".join(anchor.stripped_strings).strip()
        name = raw_name or slug.replace("-", " ").title()
        by_slug[slug] = Game(
            provider=provider_key,
            slug=slug,
            name=name,
            url=f"{parsed.scheme or 'https'}://{parsed.netloc}/games/{slug}/",
            symbol="",
        )
    return sorted(by_slug.values(), key=lambda game: (game.slug.casefold(), game.name.casefold()))


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _observed_index_actions(result: GameTestResult) -> dict[str, set[int]]:
    found: dict[str, set[int]] = {}
    root = Path(str(result.run_dir or ""))
    if not root.is_dir():
        return found
    for path in root.rglob("*request.json"):
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        action = str(payload.get("action") or "").strip().lower()
        if action not in INDEX_BRANCH_ACTIONS:
            continue
        index = payload.get("index")
        if isinstance(index, bool):
            continue
        try:
            parsed = int(index)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            found.setdefault(action, set()).add(parsed)
    return found


def apply_rubyplay_path_audit(
    result: GameTestResult,
    *,
    progress: Progress,
) -> GameTestResult:
    if result.status in {"ERROR", "CANCELADO"} or not result.run_dir:
        return result

    observed = _observed_index_actions(result)
    if not observed:
        return result

    for action, indexes in sorted(observed.items()):
        result.discovered_modes.append(
            {
                "id": f"RUBYPLAY_{action.upper()}_INDEX_DOMAIN",
                "kind": "INDEXED_CHOICE",
                "observed": True,
                "executable": True,
                "wire_command": action,
                "observed_indices": sorted(indexes),
                "coverage_required": True,
                "branch_signature": f"RUBYPLAY:{action}:index-domain",
                "required_options": ["DOMAIN_UNRESOLVED"],
                "covered_options": [],
                "reason": (
                    "el cliente demuestra que la acción usa index, pero los HAR/"
                    "contratos actuales no demuestran el dominio completo; no se "
                    "puede considerar exhaustiva una elección arbitraria"
                ),
            }
        )

    if result.status == "OK":
        result.status = "PARCIAL"
    detail = ", ".join(
        f"{action} indexes observados={sorted(indexes)}"
        for action, indexes in sorted(observed.items())
    )
    message = (
        "RubyPlay cobertura indexada pendiente: " + detail
        + "; falta dominio finito demostrado por provider."
    )
    if message not in str(result.error or ""):
        result.error = (str(result.error or "").strip() + " " + message).strip()
    progress(message)

    try:
        Path(result.run_dir, "result.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return result


class RubyPlayProvider(_RubyPlayProvider):
    """RubyPlay adapter with fail-closed exhaustive branch semantics."""

    def crawl_catalog(
        self,
        *,
        stop_event: threading.Event,
        progress: Progress,
        max_pages: int = 100,
        on_game: GameCallback | None = None,
    ) -> list[Game]:
        try:
            return super().crawl_catalog(
                stop_event=stop_event,
                progress=progress,
                max_pages=max_pages,
                on_game=on_game,
            )
        except requests.exceptions.SSLError as exc:
            progress(
                "RubyPlay catálogo: requests falló verificación TLS; "
                "reintentando con Chromium y verificación TLS normal "
                f"({type(exc).__name__})."
            )

        original_http = self.http
        browser = RubyPlayBrowserCatalogClient(self.catalog_url, timeout_s=30.0)
        try:
            browser.start()
            self.http = _BrowserCatalogSession(browser, original_http)  # type: ignore[assignment]
            try:
                return super().crawl_catalog(
                    stop_event=stop_event,
                    progress=progress,
                    max_pages=max_pages,
                    on_game=on_game,
                )
            except ValueError as exc:
                if "no se encontró query Bricks post_type=games" not in str(exc):
                    raise
                html, resolved_url = browser.fetch_catalog_html()
                games = _strict_dom_games(html, resolved_url, self.key)
                if not games:
                    raise
                reason = (
                    "DOM fallback: el catálogo actual expone targets /games/<slug>/ "
                    "pero no publica metadatos Bricks que demuestren exhaustividad"
                )
                self.set_catalog_authority(False, reason)
                progress(
                    f"RubyPlay catálogo DOM fallback: {len(games)} targets estrictos; "
                    "autoridad=no hasta demostrar cierre del listado."
                )
                if on_game is not None:
                    for game in games:
                        on_game(game)
                return games
        finally:
            self.http = original_http
            browser.close()

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        result = super().test_game(
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )
        return apply_rubyplay_path_audit(result, progress=progress)


__all__ = ["RubyPlayProvider", "apply_rubyplay_path_audit"]
