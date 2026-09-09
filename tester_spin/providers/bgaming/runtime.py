from __future__ import annotations

import hashlib
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


def response_fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def spin_remote_proof(payload: dict[str, Any]) -> dict[str, Any]:
    flow = payload.get("flow")
    outcome = payload.get("outcome")
    if not isinstance(flow, dict):
        flow = {}
    if not isinstance(outcome, dict):
        outcome = {}
    screen = outcome.get("screen")
    storage = outcome.get("storage")
    features = payload.get("features")
    if not isinstance(storage, dict):
        storage = {}
    if not isinstance(features, dict):
        features = {}
    screen_hash = ""
    if isinstance(screen, list):
        screen_hash = hashlib.sha256(
            json.dumps(
                screen,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12]
    purchased = flow.get("purchased_feature")
    purchased_name = (
        str(purchased.get("name") or "")
        if isinstance(purchased, dict)
        else ""
    )
    return {
        "round_id": flow.get("round_id"),
        "last_action_id": flow.get("last_action_id"),
        "flow_state": flow.get("state"),
        "flow_command": flow.get("command"),
        "purchased_feature": purchased_name,
        "bet": outcome.get("bet"),
        "win": outcome.get("win"),
        "balance_total": balance_total(payload),
        "screen_sha256": screen_hash,
        "storage_seed": storage.get("seed"),
        "storage_mode": storage.get("mode"),
        "freespins_issued": features.get("freespins_issued"),
        "freespins_left": features.get("freespins_left"),
        "response_sha256": response_fingerprint(payload),
    }


def balance_total(payload: dict[str, Any]) -> int | float | None:
    balance = payload.get("balance")
    if isinstance(balance, (int, float)):
        return balance
    if isinstance(balance, dict):
        wallet = balance.get("wallet")
        game = balance.get("game")
        if isinstance(wallet, (int, float)) and isinstance(game, (int, float)):
            return wallet + game

    # Switchable/container games can return wallet/game at the top level.
    wallet = payload.get("wallet")
    game = payload.get("game")
    if isinstance(wallet, (int, float)) and isinstance(game, (int, float)):
        return wallet + game
    return None


def is_switchable_container_init(data: dict[str, Any]) -> bool:
    return (
        not isinstance(data.get("options"), dict)
        and isinstance(data.get("wallet"), (int, float))
        and isinstance(data.get("game"), (int, float))
    )


def is_line_bet_init(data: dict[str, Any]) -> bool:
    options = data.get("options")
    if not isinstance(options, dict):
        return False
    line_bets = options.get("line_bets")
    lines = options.get("lines")
    return (
        isinstance(line_bets, list)
        and bool(line_bets)
        and isinstance(lines, list)
        and bool(lines)
    )


def line_bet_count(data: dict[str, Any]) -> int:
    options = data.get("options")
    if not isinstance(options, dict):
        return 0
    lines = options.get("lines")
    return len(lines) if isinstance(lines, list) else 0


def build_line_bets(data: dict[str, Any], line_bet: int | float) -> dict[str, int | float]:
    count = line_bet_count(data)
    return {str(index): line_bet for index in range(count)}


def validate_line_spin(
    data: dict[str, Any],
    *,
    requested_line_bet: int | float,
    line_count: int,
    previous_balance_total: int | float | None,
) -> tuple[list[str], int | float | None]:
    warnings: list[str] = []

    bets = data.get("bets")
    line_values = bets.get("lines") if isinstance(bets, dict) else None
    if not isinstance(line_values, dict):
        warnings.append("line-spin sin bets.lines")
    else:
        expected_keys = {str(index) for index in range(line_count)}
        actual_keys = {str(key) for key in line_values}
        if actual_keys != expected_keys:
            warnings.append(
                f"line-spin líneas={len(actual_keys)}, esperadas={line_count}"
            )
        bad = [
            key
            for key, value in line_values.items()
            if value != requested_line_bet
        ]
        if bad:
            warnings.append(f"line-spin apuestas distintas en líneas={bad[:8]}")

    game = data.get("game")
    if not isinstance(game, dict):
        warnings.append("line-spin sin game")
    else:
        if str(game.get("action") or "") != "spin":
            warnings.append(f"line-spin action inesperada={game.get('action')!r}")
        if str(game.get("state") or "") != "closed":
            warnings.append(f"line-spin state no terminal={game.get('state')!r}")

    commands = data.get("available_commands")
    if isinstance(commands, list) and "spin" not in {str(item) for item in commands}:
        warnings.append(f"line-spin sin spin disponible: {commands!r}")

    current_balance = balance_total(data)
    inferred_win: int | float | None = None
    if previous_balance_total is not None and current_balance is not None:
        total_bet = float(requested_line_bet) * float(line_count)
        inferred_win = (
            float(current_balance)
            - float(previous_balance_total)
            + total_bet
        )
        if inferred_win < -1e-9:
            warnings.append(
                f"line-spin balance imposible: win inferido={inferred_win:g}"
            )
    return warnings, inferred_win


def resolve_base_bet(data: dict[str, Any]) -> tuple[int | float | None, str]:
    options = data.get("options")
    if not isinstance(options, dict):
        return None, ""

    default_bet = options.get("default_bet")
    if isinstance(default_bet, (int, float)) and default_bet > 0:
        return default_bet, "default_bet"

    available = options.get("available_bets")
    if isinstance(available, list):
        numeric = [
            value
            for value in available
            if isinstance(value, (int, float)) and value > 0
        ]
        if numeric:
            return min(numeric), "available_bets:min"

    bet = options.get("bet")
    if isinstance(bet, (int, float)) and bet > 0:
        return bet, "options.bet"

    return None, ""


def discover_purchase_modes(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only purchase modes explicitly advertised by BGaming init.

    HAR 2026-09-09 (AlienFruits3) exposes:
      feature_options.feature_multipliers = {
        "bonus_buy": 2000,
        "bonus_chance": 30,
        "base_bet": 20,
      }

    The wire request uses options.purchased_feature=<name>.  base_bet is a
    denominator/reference, not itself a purchased feature.
    """
    options = data.get("options")
    if not isinstance(options, dict):
        return []
    feature_options = options.get("feature_options")
    if not isinstance(feature_options, dict):
        return []
    multipliers = feature_options.get("feature_multipliers")
    if not isinstance(multipliers, dict):
        return []

    base = multipliers.get("base_bet")
    base_source = "feature_multipliers.base_bet"
    if not isinstance(base, (int, float)) or base <= 0:
        # BigAtlantisFrenzy HAR: freespin_buy=8000 => x80 and
        # freespin_chance=200 => x2, with no explicit base_bet.
        # This family publishes multipliers in percent basis (100 == x1).
        base = 100
        base_source = "implicit_percent_basis"

    disabled_raw = feature_options.get("disabled_features")
    disabled: set[str] = set()
    if isinstance(disabled_raw, list):
        disabled = {str(item) for item in disabled_raw}
    elif isinstance(disabled_raw, dict):
        disabled = {
            str(key)
            for key, value in disabled_raw.items()
            if bool(value)
        }

    modes: list[dict[str, Any]] = []
    for name, raw_multiplier in multipliers.items():
        feature_name = str(name)
        if feature_name == "base_bet" or feature_name in disabled:
            continue
        if not isinstance(raw_multiplier, (int, float)) or raw_multiplier <= 0:
            continue
        modes.append(
            {
                "name": feature_name,
                "feature_multiplier": raw_multiplier,
                "base_multiplier": base,
                "base_source": base_source,
                "cost_multiplier": float(raw_multiplier) / float(base),
            }
        )
    return modes


def purchase_expected_debit(
    requested_bet: int | float,
    purchase_mode: dict[str, Any] | None,
) -> float:
    if not purchase_mode:
        return float(requested_bet)
    multiplier = purchase_mode.get("cost_multiplier")
    if not isinstance(multiplier, (int, float)) or multiplier <= 0:
        return float(requested_bet)
    return float(requested_bet) * float(multiplier)


def preselection_multiplier(data: dict[str, Any]) -> int | float | None:
    features = data.get("features")
    if not isinstance(features, dict):
        return None
    bonus_data = features.get("bonus_data")
    if not isinstance(bonus_data, dict):
        return None
    value = bonus_data.get("multiplier")
    return value if isinstance(value, (int, float)) else None


def purchase_names_equivalent(requested: str, actual: str) -> bool:
    requested_name = str(requested or "")
    actual_name = str(actual or "")
    if requested_name == actual_name:
        return True
    # Some BGaming games publish variant-specific feature multiplier keys such
    # as bonus_buy_0_chance / bonus_buy_1_chance, while flow normalizes the
    # executed feature back to purchased_feature.name=bonus_buy.
    return bool(
        actual_name
        and requested_name.startswith(actual_name + "_")
    )


def result_has_authoritative_shape(data: dict[str, Any]) -> bool:
    """Accept both server-grid and seeded-client result shapes observed in HARs."""
    outcome = data.get("outcome")
    if not isinstance(outcome, dict):
        return False
    screen = outcome.get("screen")
    if isinstance(screen, list) and bool(screen):
        return True
    storage = outcome.get("storage")
    if isinstance(storage, dict) and isinstance(storage.get("seed"), (int, float)):
        return True
    return False


def flow_available_actions(data: dict[str, Any]) -> list[str]:
    flow = data.get("flow")
    if not isinstance(flow, dict):
        return []
    actions = flow.get("available_actions")
    if not isinstance(actions, list):
        return []
    return [str(action) for action in actions if str(action)]


def flow_continuation_command(data: dict[str, Any]) -> str:
    flow = data.get("flow")
    if not isinstance(flow, dict):
        return ""
    state = str(flow.get("state") or "")
    actions = flow.get("available_actions")
    action_names = (
        {str(action) for action in actions}
        if isinstance(actions, list)
        else set()
    )
    if state == "freespins" and "freespin" in action_names:
        return "freespin"
    if state == "gamble" and "close" in action_names:
        # HAR-confirmed terminal path: collect/close instead of placing an
        # unsolicited gamble bet.
        return "close"
    if (
        state == "preselection_game"
        and "play_preselection_game" in action_names
    ):
        return "play_preselection_game"
    if state in {"", "closed", "ready", "init", "spin"}:
        return ""
    if state in action_names:
        return state
    return ""


def pending_flow_actions(data: dict[str, Any]) -> list[str]:
    actions = set(flow_available_actions(data))
    continuation = flow_continuation_command(data)
    handled = {"init", "spin", "freespin", "preselection_game"}
    if continuation:
        handled.add(continuation)
    return sorted(actions - handled)


def validate_init(data: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    legacy_lines = is_line_bet_init(data)
    if not legacy_lines and str(data.get("api_version") or "") != "2":
        warnings.append(f"api_version no observada: {data.get('api_version')!r}")

    options = data.get("options")
    if not isinstance(options, dict):
        warnings.append("init sin options")
        return warnings

    default_bet = options.get("default_bet")
    available_bets = options.get("available_bets")
    line_bets = options.get("line_bets")
    resolved_bet, _source = resolve_base_bet(data)
    if resolved_bet is None:
        warnings.append(
            "init sin apuesta utilizable: no hay default_bet ni available_bets positivos"
        )
    if (
        not isinstance(default_bet, (int, float))
        and (not isinstance(available_bets, list) or not available_bets)
        and (not isinstance(line_bets, list) or not line_bets)
    ):
        warnings.append("init sin metadata de apuestas")

    flow = data.get("flow")
    if legacy_lines:
        return warnings
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
    command: str = "spin",
    expected_debit: int | float | None = None,
    variable_layout: bool = False,
) -> list[str]:
    warnings: list[str] = []

    if str(data.get("api_version") or "") != "2":
        warnings.append(f"api_version no observada: {data.get('api_version')!r}")

    outcome = data.get("outcome")
    if not isinstance(outcome, dict):
        warnings.append(f"{command} sin outcome")
        return warnings

    actual_bet = outcome.get("bet")
    win = outcome.get("win")
    # BGaming is not consistent about outcome.bet inside zero-debit continuations.
    # For freespins/preselection, the balance delta is authoritative.
    if command == "spin" and actual_bet != requested_bet:
        warnings.append(f"bet devuelta={actual_bet!r}, solicitada={requested_bet!r}")
    if not isinstance(win, (int, float)):
        warnings.append(f"{command} sin win numérico")

    screen = outcome.get("screen")
    if isinstance(screen, list) and screen:
        # BGaming's options.layout is not a universal wire-shape contract.
        # Some valid games (e.g. UFO Pyramids / Hold&Win families) return
        # auxiliary or feature reels in outcome.screen, so a dimensions
        # mismatch is diagnostic only.  Protocol validity requires a
        # structurally usable non-empty screen, not literal layout equality.
        bad = [
            idx
            for idx, reel in enumerate(screen)
            if not isinstance(reel, list) or not reel
        ]
        if bad:
            warnings.append(
                f"screen contiene reels vacíos/inválidos={bad}"
            )
    elif not result_has_authoritative_shape(data):
        # Continuations may be balance/flow-only. FrozenFruit and Hottest666,
        # for example, return valid freespin steps without screen/seed while
        # still providing numeric win, balance and an authoritative flow.
        if command == "spin":
            warnings.append(
                f"{command} sin screen ni outcome.storage.seed autoritativos"
            )

    flow = data.get("flow")
    if not isinstance(flow, dict):
        warnings.append(f"{command} sin flow")
    else:
        flow_command = str(flow.get("command") or "")
        state = str(flow.get("state") or "")
        if flow_command != command:
            warnings.append(
                f"flow.command inesperado para {command}: {flow_command!r}"
            )
        if command == "spin":
            if (
                state not in {"closed", "freespins", "preselection_game"}
                and not flow_continuation_command(data)
            ):
                warnings.append(f"flow.state spin no observado: {state!r}")
        elif command == "freespin":
            if (
                state not in {"freespins", "closed"}
                and not flow_continuation_command(data)
            ):
                warnings.append(f"flow.state freespin no observado: {state!r}")
        elif command == "preselection_game":
            if state != "closed":
                warnings.append(
                    f"flow.state preselection_game no terminal: {state!r}"
                )
        elif command not in {"spin", "freespin"}:
            if (
                state not in {command, "closed"}
                and not flow_continuation_command(data)
            ):
                warnings.append(
                    f"flow.state inesperado para {command}: {state!r}"
                )
        elif state != "closed":
            warnings.append(f"flow.state no terminal/no observado: {state!r}")

    current_total = balance_total(data)
    debit = (
        float(expected_debit)
        if isinstance(expected_debit, (int, float))
        else (0.0 if command == "freespin" else float(actual_bet or 0))
    )
    if (
        previous_balance_total is not None
        and current_total is not None
        and isinstance(win, (int, float))
    ):
        expected = float(previous_balance_total) - debit + float(win)
        if abs(float(current_total) - expected) > 1e-9:
            warnings.append(
                f"balance inconsistente: actual={current_total}, esperado={expected}, "
                f"debito={debit}"
            )

    return warnings

