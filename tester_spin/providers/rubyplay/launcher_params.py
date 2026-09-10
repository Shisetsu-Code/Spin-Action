from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from tester_spin.providers.rubyplay import runtime as _runtime


def _single_distinct_param(
    params: dict[str, list[str]],
    name: str,
    *,
    required: bool = True,
) -> str:
    """Return one semantic query value while tolerating exact duplicates.

    RubyPlay public launch URLs can repeat a query key with the same value. That
    is still unambiguous (for example ``currency=EUR&currency=EUR``). Different
    values for the same key remain an error; taking the first value would hide a
    malformed or changed launcher contract.
    """
    values = [str(value) for value in params.get(name, [])]
    distinct = list(dict.fromkeys(values))
    if len(distinct) != 1 or (required and not distinct[0]):
        preview = distinct[:4]
        raise ValueError(
            f"RubyPlay launcher: parámetro {name!r} no unívoco "
            f"(valores={preview!r}, ocurrencias={len(values)})."
        )
    return distinct[0] if distinct else ""


def parse_launcher_url(launcher_url: str) -> _runtime.LauncherConfig:
    parsed = urlparse(launcher_url)
    params = parse_qs(parsed.query, keep_blank_values=True)

    config = _runtime.LauncherConfig(
        launcher_url=launcher_url,
        gamename=_single_distinct_param(params, "gamename"),
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
