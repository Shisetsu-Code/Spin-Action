from __future__ import annotations

import html
import re
import threading
from urllib.parse import parse_qs, urlparse

import requests

from tester_spin.providers.pragmatic import _extract_cver, _extract_launch_urls
from tester_spin.providers.pragmatic_har_states import PragmaticProvider as _HarStatePragmaticProvider


_SYMBOL_HINTS_BY_SLUG: dict[str, tuple[str, ...]] = {
    "gates-of-olympus-pop": ("vs10olymppop",),
}

_EXPLICIT_PATTERNS = (
    re.compile(r"[?&]gameSymbol=([A-Za-z0-9_-]+)", re.I),
    re.compile(r"[\"']gameSymbol[\"']\s*[:=]\s*[\"']([A-Za-z0-9_-]+)", re.I),
    re.compile(r"[\"']game_symbol[\"']\s*[:=]\s*[\"']([A-Za-z0-9_-]+)", re.I),
    re.compile(r"data-game-symbol\s*=\s*[\"']([A-Za-z0-9_-]+)", re.I),
    re.compile(r"[\"']symbol[\"']\s*[:=]\s*[\"']((?:vs|cs|bn|rng)[A-Za-z0-9_-]+)", re.I),
)

_RESOLVER_LOCAL = threading.local()


def _slug_from_game_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1].strip().lower()


def _looks_like_normal_symbol(value: str) -> bool:
    return bool(re.fullmatch(r"(?:vs|cs|bn|rng)[a-z0-9_-]{3,48}", value.strip()))


def collect_symbol_candidates(text: str, final_url: str) -> list[tuple[str, str | None, str]]:
    decoded = html.unescape(text)
    page_cver = _extract_cver(decoded)
    out: list[tuple[str, str | None, str]] = []
    seen: set[str] = set()

    def add(symbol: str, cver: str | None, evidence_url: str, *, allow_mixed: bool = False) -> None:
        symbol = symbol.strip()
        if not symbol or symbol in seen:
            return
        if not allow_mixed and not _looks_like_normal_symbol(symbol):
            return
        seen.add(symbol)
        out.append((symbol, cver, evidence_url))

    for launch_url in _extract_launch_urls(decoded, final_url):
        parsed = urlparse(launch_url)
        symbol = (parse_qs(parsed.query).get("gameSymbol") or [""])[0].strip()
        if symbol:
            add(
                symbol,
                _extract_cver(launch_url) or page_cver,
                launch_url,
                allow_mixed=True,
            )

    slug = _slug_from_game_url(final_url)
    for symbol in _SYMBOL_HINTS_BY_SLUG.get(slug, ()):
        add(symbol, page_cver, final_url)

    for pattern in _EXPLICIT_PATTERNS:
        for match in pattern.finditer(decoded):
            add(match.group(1), page_cver, final_url)

    for symbol in re.findall(r"\b(?:vs|cs|bn|rng)[a-z0-9_-]{3,48}\b", decoded):
        add(symbol, page_cver, final_url)

    return out


def _resolver_session(provider) -> requests.Session:
    """Return one requests.Session per execution thread.

    requests.Session carries mutable cookies, adapters and connection-pool state.
    Keeping it thread-local prevents concurrent game discovery from sharing that
    mutable state while still allowing each worker to reuse keep-alive connections.
    """
    session = getattr(_RESOLVER_LOCAL, "session", None)
    owner = getattr(_RESOLVER_LOCAL, "owner", None)
    if session is not None and owner is provider:
        return session

    session = requests.Session()
    session.headers.update(dict(provider.http.headers))
    try:
        session.cookies.update(provider.http.cookies.get_dict())
    except Exception:
        pass
    _RESOLVER_LOCAL.session = session
    _RESOLVER_LOCAL.owner = provider
    return session


class PragmaticProvider(_HarStatePragmaticProvider):
    """HAR state machine with bootstrap-validated provider-symbol resolution."""

    def _resolve_symbol_http(self, source_url: str, timeout_s: float) -> tuple[str, str | None, str]:
        response = _resolver_session(self).get(source_url, timeout=timeout_s, allow_redirects=True)
        response.raise_for_status()
        final_url = response.url
        candidates = collect_symbol_candidates(response.text, final_url)
        if not candidates:
            raise RuntimeError("la página HTTP del juego no expone candidatos de provider_internal_id utilizables")

        failures: list[str] = []
        for symbol, cver, evidence_url in candidates:
            try:
                probe = self._http_bootstrap(
                    source_url,
                    symbol,
                    cver,
                    self.base_bet,
                    timeout_s,
                )
            except Exception as exc:
                failures.append(f"{symbol}:{type(exc).__name__}")
                continue
            try:
                return symbol, probe.cver or cver, probe.launch_url or evidence_url
            finally:
                probe.session.close()

        preview = ", ".join(failures[:8]) or "sin detalle"
        raise RuntimeError(
            "ningún provider_internal_id candidato superó bootstrap; "
            f"candidatos={len(candidates)}; fallos={preview}"
        )
