from __future__ import annotations

import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from tester_spin.providers.rubyplay import runtime as _runtime


def _single_distinct_param(
    params: dict[str, list[str]],
    name: str,
    *,
    required: bool = True,
) -> str:
    """Return one semantic query value while tolerating exact duplicates."""
    values = [str(value) for value in params.get(name, [])]
    distinct = list(dict.fromkeys(values))
    if len(distinct) != 1 or (required and not distinct[0]):
        preview = distinct[:4]
        raise ValueError(
            f"RubyPlay launcher: parámetro {name!r} no unívoco "
            f"(valores={preview!r}, ocurrencias={len(values)})."
        )
    return distinct[0] if distinct else ""


def _collapse_exact_doubled_gamename(value: str) -> str:
    """Collapse only exact provider-ID duplication, e.g. rp_160rp_160 -> rp_160."""
    text = str(value or "").strip()
    match = re.fullmatch(r"(rp_[A-Za-z0-9_-]+)\1", text, re.I)
    return match.group(1) if match else text


def _replace_query_param(url: str, name: str, value: str) -> str:
    parsed = urlparse(url)
    items = parse_qsl(parsed.query, keep_blank_values=True)
    replaced: list[tuple[str, str]] = []
    seen = False
    for key, current in items:
        if key == name:
            if not seen:
                replaced.append((key, value))
                seen = True
            continue
        replaced.append((key, current))
    if not seen:
        replaced.append((name, value))
    return urlunparse(parsed._replace(query=urlencode(replaced, doseq=True)))


def parse_launcher_url(launcher_url: str) -> _runtime.LauncherConfig:
    parsed = urlparse(launcher_url)
    params = parse_qs(parsed.query, keep_blank_values=True)

    raw_gamename = _single_distinct_param(params, "gamename")
    gamename = _collapse_exact_doubled_gamename(raw_gamename)
    canonical_launcher = (
        _replace_query_param(launcher_url, "gamename", gamename)
        if gamename != raw_gamename
        else launcher_url
    )

    config = _runtime.LauncherConfig(
        launcher_url=canonical_launcher,
        gamename=gamename,
        operator=_single_distinct_param(params, "operator"),
        server_url=_single_distinct_param(params, "server_url"),
        currency=_single_distinct_param(params, "currency"),
        mode=_single_distinct_param(params, "mode"),
        lang=_single_distinct_param(params, "lang", required=False) or "en",
    )
    server = urlparse(config.server_url)
    if server.scheme not in {"http", "https"} or not server.netloc:
        raise ValueError("RubyPlay launcher: server_url inválido.")
    return config


def install_runtime_launcher_parser() -> None:
    """Install the stricter semantic parser in the RubyPlay runtime module."""
    _runtime.parse_launcher_url = parse_launcher_url
