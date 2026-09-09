from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


SENSITIVE_OPTION_KEYS = {
    "play_token",
    "csrfTokenHeaderValue",
    "drops_token",
    "profile_token",
    "gamelist_token",
    "challenges_token",
    "quests_token",
    "shop_token",
}


@dataclass(slots=True)
class BGamingRuntime:
    session: requests.Session
    launch_url: str
    api_url: str
    identifier: str
    csrf_header_name: str
    csrf_header_value: str
    options: dict[str, Any]
    round_series_id: int


def extract_options(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "html.parser")
    decoder = json.JSONDecoder()

    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        marker = "window.__OPTIONS__"
        pos = text.find(marker)
        if pos < 0:
            continue
        eq = text.find("=", pos + len(marker))
        if eq < 0:
            continue
        payload = text[eq + 1 :].lstrip()
        try:
            value, _ = decoder.raw_decode(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value

    raise ValueError("BGaming: window.__OPTIONS__ no encontrado en el HTML del juego.")


def sanitize_options(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_lower = str(key).casefold()
            if key in SENSITIVE_OPTION_KEYS or (
                "token" in key_lower and key != "csrfTokenHeaderName"
            ):
                clean[key] = "<redacted>"
            elif key == "api":
                clean[key] = sanitize_session_url(str(item))
            elif key == "websocket_url":
                clean[key] = sanitize_session_url(str(item))
            else:
                clean[key] = sanitize_options(item)
        return clean
    if isinstance(value, list):
        return [sanitize_options(item) for item in value]
    return value


def sanitize_error_text(text: str) -> str:
    value = str(text or "")
    value = re.sub(
        r"([?&](?:launch_token|play_token|token)=)[^&\s]+",
        r"\1<redacted>",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"(/api/[^/\s]+/[^/\s]+/)[^/?#\s]+",
        r"\1<session>",
        value,
        flags=re.IGNORECASE,
    )
    return value


def sanitize_session_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "api" in parts and len(parts) >= 4:
        parts[-1] = "<session>"
    path = "/" + "/".join(parts) if parts else parsed.path
    return parsed._replace(path=path, query="").geturl()


def bootstrap_game(
    session: requests.Session,
    demo_url: str,
    *,
    timeout_s: float,
) -> BGamingRuntime:
    response = session.get(demo_url, timeout=timeout_s, allow_redirects=True)
    response.raise_for_status()
    options = extract_options(response.text)

    api_url = str(options.get("api") or "").strip()
    identifier = str(options.get("identifier") or "").strip()
    csrf_name = str(options.get("csrfTokenHeaderName") or "").strip()
    csrf_value = str(options.get("csrfTokenHeaderValue") or "").strip()

    if not api_url or not identifier:
        raise ValueError("BGaming: bootstrap incompleto; faltan api/identifier.")
    if not csrf_name or not csrf_value:
        raise ValueError("BGaming: bootstrap incompleto; faltan datos CSRF.")

    return BGamingRuntime(
        session=session,
        launch_url=response.url,
        api_url=api_url,
        identifier=identifier,
        csrf_header_name=csrf_name,
        csrf_header_value=csrf_value,
        options=options,
        round_series_id=int(time.time() * 1000),
    )


def post_command(
    runtime: BGamingRuntime,
    command: str,
    *,
    timeout_s: float,
    options: dict[str, Any] | None = None,
    extra_data: dict[str, Any] | None = None,
) -> tuple[requests.Response, dict[str, Any], dict[str, Any]]:
    payload: dict[str, Any] = {"command": command}
    if options is not None:
        payload["options"] = options
    payload["extra_data"] = (
        dict(extra_data)
        if extra_data is not None
        else {"round_series_id": runtime.round_series_id}
    )

    parsed = urlparse(runtime.api_url)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": f"{parsed.scheme}://{parsed.netloc}",
        "Referer": runtime.launch_url,
        runtime.csrf_header_name: runtime.csrf_header_value,
    }
    response = runtime.session.post(
        runtime.api_url,
        json=payload,
        headers=headers,
        timeout=timeout_s,
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError("BGaming: respuesta no JSON del endpoint de juego.") from exc
    if not isinstance(data, dict):
        raise ValueError("BGaming: respuesta JSON inesperada.")
    return response, payload, data


def balance_total(payload: dict[str, Any]) -> int | float | None:
    balance = payload.get("balance")
    if isinstance(balance, (int, float)):
        return balance
    if not isinstance(balance, dict):
        return None
    wallet = balance.get("wallet")
    game = balance.get("game")
    if not isinstance(wallet, (int, float)) or not isinstance(game, (int, float)):
        return None
    return wallet + game


def validate_init(data: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if str(data.get("api_version") or "") != "2":
        warnings.append(f"api_version no observada: {data.get('api_version')!r}")

    options = data.get("options")
    if not isinstance(options, dict):
        warnings.append("init sin options")
        return warnings

    default_bet = options.get("default_bet")
    available_bets = options.get("available_bets")
    if not isinstance(default_bet, (int, float)):
        warnings.append("init sin default_bet numérico")
    if not isinstance(available_bets, list) or not available_bets:
        warnings.append("init sin available_bets")

    flow = data.get("flow")
    if not isinstance(flow, dict):
        warnings.append("init sin flow")
    else:
        if str(flow.get("command") or "") != "init":
            warnings.append(f"flow.command init inesperado: {flow.get('command')!r}")
        state = str(flow.get("state") or "")
        if state not in {"ready", "closed"}:
            warnings.append(f"flow.state init no observado: {state!r}")
        actions = flow.get("available_actions")
        if isinstance(actions, list) and "spin" not in actions:
            warnings.append(f"spin no disponible tras init: {actions!r}")

    return warnings


def validate_spin(
    data: dict[str, Any],
    *,
    requested_bet: int | float,
    previous_balance_total: int | float | None,
    expected_reels: int | None,
    expected_rows: int | None,
) -> list[str]:
    warnings: list[str] = []

    if str(data.get("api_version") or "") != "2":
        warnings.append(f"api_version no observada: {data.get('api_version')!r}")

    outcome = data.get("outcome")
    if not isinstance(outcome, dict):
        warnings.append("spin sin outcome")
        return warnings

    actual_bet = outcome.get("bet")
    win = outcome.get("win")
    if actual_bet != requested_bet:
        warnings.append(f"bet devuelta={actual_bet!r}, solicitada={requested_bet!r}")
    if not isinstance(win, (int, float)):
        warnings.append("spin sin win numérico")

    screen = outcome.get("screen")
    if not isinstance(screen, list) or not screen:
        warnings.append("spin sin screen")
    else:
        if expected_reels is not None and len(screen) != expected_reels:
            warnings.append(
                f"screen reels={len(screen)}, esperados={expected_reels}"
            )
        if expected_rows is not None:
            bad = [
                idx
                for idx, reel in enumerate(screen)
                if not isinstance(reel, list) or len(reel) != expected_rows
            ]
            if bad:
                warnings.append(f"screen rows inesperadas en reels={bad}")

    flow = data.get("flow")
    if not isinstance(flow, dict):
        warnings.append("spin sin flow")
    else:
        command = str(flow.get("command") or "")
        state = str(flow.get("state") or "")
        actions = flow.get("available_actions")
        if command != "spin":
            warnings.append(f"flow.command no observado: {command!r}")
        if state != "closed":
            warnings.append(f"flow.state no terminal/no observado: {state!r}")
        if isinstance(actions, list):
            unknown = sorted(set(str(action) for action in actions) - {"init", "spin"})
            if unknown:
                warnings.append(f"acciones BGaming no clasificadas: {unknown}")

    current_total = balance_total(data)
    if (
        previous_balance_total is not None
        and current_total is not None
        and isinstance(actual_bet, (int, float))
        and isinstance(win, (int, float))
    ):
        expected = previous_balance_total - actual_bet + win
        if abs(float(current_total) - float(expected)) > 1e-9:
            warnings.append(
                f"balance inconsistente: actual={current_total}, esperado={expected}"
            )

    return warnings
