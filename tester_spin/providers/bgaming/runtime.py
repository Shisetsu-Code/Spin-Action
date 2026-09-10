from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from tester_spin.providers.bgaming.contracts import (
    CONTINUATION_BY_STATE,
    SAFE_CONTINUATION_COMMANDS,
)


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
    script_urls: list[str] = field(default_factory=list)
    request_extra_data: dict[str, Any] = field(default_factory=dict)


def extract_script_urls(html: str, base_url: str) -> list[str]:
    """Collect JavaScript resources actually referenced by the launch page.

    Runtime discovery uses these URLs as protocol evidence. The list is
    structural provider data; no game-name/slug routing is involved.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    urls: list[str] = []
    seen: set[str] = set()
    for node in soup.find_all("script", src=True):
        raw = str(node.get("src") or "").strip()
        if not raw:
            continue
        url = urljoin(base_url, raw)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


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


def is_demo_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").casefold()
    if not (host == "bgaming-network.com" or host.endswith(".bgaming-network.com")):
        return False
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3:
        return False
    return parts[0].casefold() in {"play", "games"}


def resolve_fresh_demo_url(
    session: requests.Session,
    public_or_demo_url: str,
    *,
    timeout_s: float,
) -> str:
    """Resolve and validate a current demo launch URL in memory."""
    source = str(public_or_demo_url or "").strip()
    if is_demo_url(source):
        return source
    if not source:
        return ""

    response = session.get(source, timeout=timeout_s, allow_redirects=True)
    response.raise_for_status()
    if is_demo_url(response.url):
        try:
            extract_options(response.text)
        except ValueError:
            pass
        else:
            return response.url

    soup = BeautifulSoup(response.text or "", "html.parser")
    candidates: list[str] = []
    for node in soup.select("a[href], iframe[src]"):
        raw = str(node.get("href") or node.get("src") or "").strip()
        if not raw:
            continue
        candidate = urljoin(response.url, raw)
        if is_demo_url(candidate):
            candidates.append(candidate)

    normalized_html = (response.text or "").replace("\\/", "/")
    normalized_html = normalized_html.replace("\\u002F", "/").replace("\\u002f", "/")
    for match in re.findall(
        r"https?://(?:[A-Za-z0-9.-]+\\.)?bgaming-network\\.com/[^\"'<>\\s]+",
        normalized_html,
        flags=re.IGNORECASE,
    ):
        candidate = match.rstrip("),.;]")
        if is_demo_url(candidate):
            candidates.append(candidate)

    candidates = list(dict.fromkeys(candidates))
    candidates.sort(
        key=lambda value: (
            0 if urlparse(value).path.startswith("/play/") else 1,
            -len(urlparse(value).path),
        )
    )

    # Never trust presence in page source alone. A stale/truncated candidate
    # must not be returned as a demo merely because it looks like one.
    for candidate in candidates:
        try:
            probe = session.get(candidate, timeout=timeout_s, allow_redirects=True)
            probe.raise_for_status()
            if not is_demo_url(probe.url):
                continue
            extract_options(probe.text)
        except Exception:
            continue
        return probe.url

    return ""

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
        script_urls=extract_script_urls(response.text, response.url),
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
    merged_extra_data: dict[str, Any] = {
        "round_series_id": runtime.round_series_id,
    }
    merged_extra_data.update(runtime.request_extra_data)
    if extra_data is not None:
        merged_extra_data.update(extra_data)
    payload["extra_data"] = merged_extra_data

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


def legacy_safe_terminal_command(data: dict[str, Any]) -> str:
    """Return a non-wagering legacy command that safely closes the current round.

    Legacy line-bet games can expose an optional card/gamble state after a win.
    We never enter that wager. When the server itself advertises `finish` and
    no new `spin` is available, `finish` is the provider-level terminal path.
    """
    game = data.get("game")
    commands = data.get("available_commands")
    if not isinstance(game, dict) or not isinstance(commands, list):
        return ""
    command_names = {str(item) for item in commands}
    state = str(game.get("state") or "")
    if state != "closed" and "finish" in command_names and "spin" not in command_names:
        return "finish"
    return ""


def validate_line_spin(
    data: dict[str, Any],
    *,
    requested_line_bet: int | float,
    line_count: int,
    previous_balance_total: int | float | None,
    allow_safe_finish: bool = False,
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

    safe_finish = legacy_safe_terminal_command(data) if allow_safe_finish else ""
    game = data.get("game")
    if not isinstance(game, dict):
        warnings.append("line-spin sin game")
    else:
        if str(game.get("action") or "") != "spin":
            warnings.append(f"line-spin action inesperada={game.get('action')!r}")
        if str(game.get("state") or "") != "closed" and not safe_finish:
            warnings.append(f"line-spin state no terminal={game.get('state')!r}")

    commands = data.get("available_commands")
    if (
        isinstance(commands, list)
        and "spin" not in {str(item) for item in commands}
        and not safe_finish
    ):
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


def _provider_script_url(runtime: BGamingRuntime, url: str) -> bool:
    """Accept launch scripts only from BGaming-controlled/declared origins."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.casefold()

    declared_hosts: set[str] = set()
    for candidate in (
        runtime.launch_url,
        runtime.api_url,
        str(runtime.options.get("resources_path") or ""),
        str(runtime.options.get("game_bundle_source") or ""),
        str(runtime.options.get("games_loader_source") or ""),
    ):
        candidate_host = urlparse(candidate).hostname
        if candidate_host:
            declared_hosts.add(candidate_host.casefold())

    return (
        host in declared_hosts
        or host == "bgaming-network.com"
        or host.endswith(".bgaming-network.com")
    )


def _bundle_contract_score(text: str) -> int:
    value = text or ""
    score = 0
    markers = (
        ("additionalSpinOptions", 12),
        ("extraDataOptions", 12),
        ("api_version", 10),
        ("purchased_feature", 10),
        ("round_series_id", 8),
        ("feature_multipliers", 8),
        ("available_actions", 6),
        ("last_action_id", 6),
        ("flow", 4),
        ("outcome", 4),
    )
    for marker, weight in markers:
        if marker in value:
            score += weight
    if re.search(r"['\"]command['\"]\\s*:", value):
        score += 5
    if re.search(r"['\"]spin['\"]", value):
        score += 2
    return score

def _runtime_bundle_candidates(
    runtime: BGamingRuntime,
    *,
    timeout_s: float,
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[str]:
    candidates: list[str] = []
    configured = str(runtime.options.get("game_bundle_source") or "").strip()
    resources_path = str(runtime.options.get("resources_path") or "").rstrip("/")
    loader_url = str(runtime.options.get("games_loader_source") or "").strip()

    if loader_url:
        try:
            response = runtime.session.get(loader_url, timeout=timeout_s)
            response.raise_for_status()
            loader_text = response.text
            if diagnostics is not None:
                diagnostics.append(
                    {
                        "kind": "loader",
                        "url": sanitize_session_url(loader_url),
                        "status": int(getattr(response, "status_code", 200)),
                        "ok": True,
                    }
                )
        except Exception as exc:
            loader_text = ""
            if diagnostics is not None:
                diagnostics.append(
                    {
                        "kind": "loader",
                        "url": sanitize_session_url(loader_url),
                        "status": int(exc.response.status_code)
                        if isinstance(exc, requests.HTTPError) and exc.response is not None
                        else None,
                        "ok": False,
                        "error": sanitize_error_text(
                            f"{type(exc).__name__}: {exc}"
                        ),
                    }
                )

        if loader_text:
            version = ""
            patterns = [
                r'res:\w+="([^"]+)"',
                r"res:\w+='([^']+)'",
                r'res:t="([^"]+)"',
                r"res:t='([^']+)'",
                r'res:"([^"]+)"',
                r"res:'([^']+)'",
            ]
            for pattern in patterns:
                match = re.search(pattern, loader_text)
                if match:
                    version = match.group(1).strip()
                    break
            if version and resources_path:
                candidates.append(f"{resources_path}/{version}/bundle.js")

    if configured:
        candidates.append(configured)

    # Prefer scripts the launch page actually loaded over guessed filenames,
    # but never treat third-party analytics as provider protocol evidence.
    for script_url in runtime.script_urls:
        if script_url and _provider_script_url(runtime, script_url):
            candidates.append(script_url)
        elif script_url and diagnostics is not None:
            diagnostics.append(
                {
                    "kind": "script-skip",
                    "url": sanitize_session_url(script_url),
                    "reason": "third-party-origin",
                }
            )

    out: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _parse_safe_js_scalar(value: str) -> Any:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty literal")
    if (
        (raw.startswith('"') and raw.endswith('"'))
        or (raw.startswith("'") and raw.endswith("'"))
    ):
        return raw[1:-1]
    lowered = raw.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return float(raw) if "." in raw else int(raw)
    raise ValueError("non-literal JavaScript value")


def _safe_extra_data_key(key: str) -> bool:
    lowered = str(key or "").casefold()
    if not lowered:
        return False
    forbidden = ("token", "session", "csrf", "secret", "password", "key")
    return not any(marker in lowered for marker in forbidden)


def discover_client_extra_data_defaults(bundle: str) -> dict[str, Any]:
    """Extract literal provider request defaults from the loaded client.

    Only values nested under extraDataOptions.extra_data are accepted. Dynamic
    expressions are ignored rather than evaluated, keeping discovery deterministic.
    """
    found: dict[str, Any] = {}
    patterns = [
        r"extraDataOptions\s*=\s*\{\s*extra_data\s*:\s*\{([^{}]{1,800})\}\s*\}",
        r'extraDataOptions\s*=\s*\{\s*["\']extra_data["\']\s*:\s*\{([^{}]{1,800})\}\s*\}',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, bundle or ""):
            body = match.group(1)
            for pair in re.finditer(
                r'(?:["\']?)([A-Za-z_][A-Za-z0-9_]*)(?:["\']?)\s*:\s*([^,}]+)',
                body,
            ):
                key = str(pair.group(1) or "").strip()
                if not _safe_extra_data_key(key):
                    continue
                try:
                    value = _parse_safe_js_scalar(pair.group(2))
                except ValueError:
                    continue
                found[key] = value
    return found


def _dedupe_values(values: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        if value in ("", None):
            continue
        key = (type(value).__name__, str(value))
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def discover_additional_spin_option_choices(bundle: str) -> dict[str, list[Any]]:
    """Discover finite client-side choices for additionalSpinOptions fields."""
    fields = list(
        dict.fromkeys(
            re.findall(
                r"additionalSpinOptions\.([A-Za-z_][A-Za-z0-9_]*)",
                bundle or "",
            )
        )
    )
    discovered: dict[str, list[Any]] = {}

    for field in fields:
        values: list[Any] = []

        for match in re.finditer(
            rf"additionalSpinOptions\.{re.escape(field)}\s*=\s*"
            r"([\"'][^\"']{0,80}[\"'])",
            bundle or "",
        ):
            raw = match.group(1)
            values.append(raw[1:-1])

        for match in re.finditer(
            rf"additionalSpinOptions\.{re.escape(field)}\s*=\s*"
            r"[^,;]{0,220}\?\s*[\"']([^\"']{0,80})[\"']"
            r"\s*:\s*[\"']([^\"']{0,80})[\"']",
            bundle or "",
        ):
            values.extend([match.group(1), match.group(2)])

        for occurrence in re.finditer(
            rf"additionalSpinOptions\.{re.escape(field)}\s*=",
            bundle or "",
        ):
            left = (bundle or "")[
                max(0, occurrence.start() - 1200) : occurrence.start()
            ]
            methods = list(
                re.finditer(
                    r"([A-Za-z_$][A-Za-z0-9_$]*)"
                    r"\(([A-Za-z_$][A-Za-z0-9_$]*)(?:,[^)]*)?\)\{",
                    left,
                )
            )
            if not methods:
                continue

            method_match = methods[-1]
            method_name = method_match.group(1)
            parameter = method_match.group(2)
            rhs = (bundle or "")[occurrence.end() : occurrence.end() + 120]
            if not re.search(rf"\b{re.escape(parameter)}\b", rhs):
                continue

            stringify = bool(
                re.search(
                    rf'(?:""\s*\+\s*{re.escape(parameter)}'
                    rf"|''\s*\+\s*{re.escape(parameter)}"
                    rf"|String\(\s*{re.escape(parameter)}\s*\))",
                    rhs,
                )
            )
            numeric_literals: list[str] = []
            numeric_literals.extend(
                re.findall(
                    re.escape(method_name) + r"`([0-9]+(?:\.[0-9]+)?)",
                    bundle or "",
                )
            )
            numeric_literals.extend(
                re.findall(
                    re.escape(method_name)
                    + r"\(\s*([0-9]+(?:\.[0-9]+)?)\s*[,)]",
                    bundle or "",
                )
            )
            for raw_number in numeric_literals:
                if stringify:
                    values.append(raw_number)
                elif "." in raw_number:
                    values.append(float(raw_number))
                else:
                    values.append(int(raw_number))

        clean = _dedupe_values(values)
        if clean:
            discovered[field] = clean

    return discovered


def discover_effective_bet_multipliers(bundle: str) -> dict[str, float]:
    """Discover an unambiguous BET_BY_SPECIAL_LVL map from provider JS."""
    references = list(
        dict.fromkeys(
            re.findall(
                r"\bBET_BY_SPECIAL_LVL\s*=\s*"
                r"([A-Za-z_$][A-Za-z0-9_$]*)",
                bundle or "",
            )
        )
    )
    maps: list[dict[str, float]] = []
    for variable in references:
        match = re.search(
            rf"\b{re.escape(variable)}\s*=\s*\{{([^{{}}]{{1,500}})\}}",
            bundle or "",
        )
        if not match:
            continue
        pairs = re.findall(
            r"(?:[\"']?)(\d+)(?:[\"']?)\s*:\s*"
            r"(-?\d+(?:\.\d+)?)",
            match.group(1),
        )
        if len(pairs) < 2:
            continue
        maps.append({key: float(value) for key, value in pairs})

    unique: dict[tuple[tuple[str, float], ...], dict[str, float]] = {}
    for mapping in maps:
        unique[tuple(sorted(mapping.items()))] = mapping
    if len(unique) != 1:
        return {}
    return next(iter(unique.values()))


def effective_bet_for_options(
    requested_bet: int | float,
    *,
    selector_field: str,
    multipliers: dict[str, float],
    options: dict[str, Any],
) -> float:
    if not selector_field or not multipliers:
        return float(requested_bet)
    selected = options.get(selector_field)
    multiplier = multipliers.get(str(selected))
    if not isinstance(multiplier, (int, float)) or multiplier <= 0:
        return float(requested_bet)
    return float(requested_bet) * float(multiplier)

def _provider_script_references(
    runtime: BGamingRuntime,
    *,
    parent_url: str,
    text: str,
) -> list[str]:
    """Discover static provider-owned JS references from another JS resource."""
    refs: list[str] = []
    patterns = [
        r'["\']([^"\']+\.js(?:\?[^"\']*)?)["\']',
        r'import\(\s*["\']([^"\']+)["\']\s*\)',
    ]
    seen: set[str] = set()
    for pattern in patterns:
        for raw in re.findall(pattern, text or ""):
            value = str(raw or "").strip()
            if not value:
                continue
            url = urljoin(parent_url, value)
            if not _provider_script_url(runtime, url):
                continue
            if url in seen:
                continue
            seen.add(url)
            refs.append(url)
    return refs


def _collect_provider_script_contracts(
    runtime: BGamingRuntime,
    *,
    timeout_s: float,
    seeds: list[str],
    diagnostics: list[dict[str, Any]],
    max_depth: int = 2,
    max_scripts: int = 32,
    max_total_bytes: int = 12 * 1024 * 1024,
) -> list[tuple[int, str, str]]:
    """Walk a bounded graph of provider-owned scripts and keep protocol-bearing ones."""
    queue: list[tuple[str, int]] = [(url, 0) for url in seeds if url]
    seen: set[str] = set()
    contracts: list[tuple[int, str, str]] = []
    total_bytes = 0

    while queue and len(seen) < max_scripts and total_bytes < max_total_bytes:
        url, depth = queue.pop(0)
        if url in seen or not _provider_script_url(runtime, url):
            continue
        seen.add(url)
        try:
            response = runtime.session.get(url, timeout=timeout_s)
            response.raise_for_status()
            script_text = response.text or ""
            encoded_size = len(script_text.encode("utf-8", errors="replace"))
            total_bytes += encoded_size
            score = _bundle_contract_score(script_text)
            diagnostics.append(
                {
                    "kind": "bundle",
                    "url": sanitize_session_url(url),
                    "status": int(getattr(response, "status_code", 200)),
                    "ok": True,
                    "bytes": encoded_size,
                    "contract_score": score,
                    "depth": depth,
                }
            )
        except Exception as exc:
            diagnostics.append(
                {
                    "kind": "bundle",
                    "url": sanitize_session_url(url),
                    "status": int(exc.response.status_code)
                    if isinstance(exc, requests.HTTPError) and exc.response is not None
                    else None,
                    "ok": False,
                    "error": sanitize_error_text(f"{type(exc).__name__}: {exc}"),
                    "depth": depth,
                }
            )
            continue

        if score > 0:
            contracts.append((score, url, script_text))

        if depth < max_depth:
            for child in _provider_script_references(
                runtime,
                parent_url=url,
                text=script_text,
            ):
                if child not in seen:
                    queue.append((child, depth + 1))

    return contracts

def discover_api_v2_wire_profile(
    runtime: BGamingRuntime,
    *,
    timeout_s: float,
) -> dict[str, Any]:
    """Discover wire options from BGaming-controlled client scripts.

    Multiple scripts may participate in request construction. Provider-owned
    scripts with protocol markers are combined; HTML script order is not an
    authority signal.
    """
    diagnostics: list[dict[str, Any]] = []
    seeds = _runtime_bundle_candidates(
        runtime,
        timeout_s=timeout_s,
        diagnostics=diagnostics,
    )
    contract_parts = _collect_provider_script_contracts(
        runtime,
        timeout_s=timeout_s,
        seeds=seeds,
        diagnostics=diagnostics,
    )
    contract_parts.sort(key=lambda item: item[0], reverse=True)
    bundle = "\n".join(item[2] for item in contract_parts)
    source = contract_parts[0][1] if contract_parts else ""

    purchase_features: set[str] = set()
    for match in re.finditer(
        r"(?:['\"]?purchased_feature['\"]?\s*[:=]\s*['\"])([A-Za-z0-9_\-]+)",
        bundle,
    ):
        value = str(match.group(1) or "").strip()
        if value:
            purchase_features.add(value)

    request_extra_data = discover_client_extra_data_defaults(bundle)

    spin_option_choices = discover_additional_spin_option_choices(bundle)
    effective_bet_multipliers = discover_effective_bet_multipliers(bundle)

    required_option_fields = sorted(
        {
            str(match)
            for match in re.findall(
                r"additionalSpinOptions\.([A-Za-z_][A-Za-z0-9_]*)",
                bundle,
            )
            if str(match) not in {"purchased_feature", "purchased_feature_level"}
        }
    )

    profile: dict[str, Any] = {
        "spin_options": {},
        "request_extra_data": dict(request_extra_data),
        "purchase_features": sorted(purchase_features),
        "dynamic_purchased_feature": bool(
            re.search(
                r"additionalSpinOptions\.purchased_feature(?:\b|\s*=)",
                bundle,
            )
        ),
        "purchase_feature_level_supported": bool(
            re.search(
                r"additionalSpinOptions\.purchased_feature_level(?:\b|\s*=)",
                bundle,
            )
        ),
        "required_option_fields": required_option_fields,
        "spin_option_choices": spin_option_choices,
        "effective_bet_multipliers": effective_bet_multipliers,
        "source": sanitize_session_url(source) if source else "",
        "bundle_sha256": (
            hashlib.sha256(bundle.encode("utf-8", errors="replace")).hexdigest()
            if bundle
            else ""
        ),
        "diagnostics": diagnostics,
    }
    if not bundle:
        return profile

    if "additionalSpinOptions.mode" in bundle:
        default_mode = ""
        patterns = [
            r'this\.linesCount=this\.linesCount\|\|"([0-9]+)"',
            r"this\.linesCount=this\.linesCount\|\|'([0-9]+)'",
        ]
        for pattern in patterns:
            match = re.search(pattern, bundle)
            if match:
                default_mode = match.group(1)
                break
        if default_mode:
            profile["spin_options"]["mode"] = default_mode
            profile["mode"] = default_mode
            profile["kind"] = "selectable-lines-mode"

    return profile

def infer_missing_wire_options(
    response: requests.Response,
    init_data: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Infer missing request options only from explicit HTTP validation evidence.

    Candidate values come from the init contract; a field is returned only when
    the 4xx response itself names that field. This prevents the executor from
    guessing per-game options from layout alone.
    """
    evidence_parts: list[str] = []
    try:
        payload = response.json()
    except Exception:
        payload = None
    if payload is not None:
        try:
            evidence_parts.append(
                json.dumps(payload, ensure_ascii=False, sort_keys=True)
            )
        except Exception:
            evidence_parts.append(str(payload))
    raw_text = str(getattr(response, "text", "") or "")
    if raw_text:
        evidence_parts.append(raw_text)
    evidence = "\n".join(evidence_parts)
    lowered = evidence.casefold()

    options = init_data.get("options")
    if not isinstance(options, dict):
        return {}, sanitize_error_text(evidence)[:1200]

    candidate_sources: list[dict[str, Any]] = []
    layout = options.get("layout")
    if isinstance(layout, dict):
        candidate_sources.append(layout)

    # Scalar init options are eligible too, except values already handled by
    # the normal request builder such as bet/default_bet.
    scalar_options = {
        str(key): value
        for key, value in options.items()
        if isinstance(value, (str, int, float, bool))
        and str(key) not in {"bet", "default_bet"}
    }
    candidate_sources.append(scalar_options)

    inferred: dict[str, Any] = {}
    for source in candidate_sources:
        for key, value in source.items():
            field = str(key).strip()
            if not field or field in inferred:
                continue
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(field.casefold())}(?![A-Za-z0-9_])", lowered):
                inferred[field] = value

    return inferred, sanitize_error_text(evidence)[:1200]


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
        # Do not assume a denominator when the provider does not publish one.
        # The executor learns the effective cost from the authoritative balance
        # delta after the first successful demo purchase.
        base = None
        base_source = "unreported;learn-from-balance"

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

        level_values: list[tuple[str | None, int | float]] = []
        if isinstance(raw_multiplier, (int, float)) and raw_multiplier > 0:
            level_values.append((None, raw_multiplier))
        elif isinstance(raw_multiplier, dict):
            for raw_level, level_multiplier in raw_multiplier.items():
                if (
                    isinstance(level_multiplier, (int, float))
                    and level_multiplier > 0
                ):
                    level_values.append((str(raw_level), level_multiplier))

        for level, level_multiplier in level_values:
            modes.append(
                {
                    "name": feature_name,
                    "level": level,
                    "feature_multiplier": level_multiplier,
                    "base_multiplier": base,
                    "base_source": base_source,
                    "cost_multiplier": (
                        float(level_multiplier) / float(base)
                        if isinstance(base, (int, float)) and base > 0
                        else None
                    ),
                }
            )
    return modes


def purchase_expected_debit(
    requested_bet: int | float,
    purchase_mode: dict[str, Any] | None,
) -> float | None:
    if not purchase_mode:
        return float(requested_bet)
    multiplier = purchase_mode.get("cost_multiplier")
    if not isinstance(multiplier, (int, float)) or multiplier <= 0:
        return None
    return float(requested_bet) * float(multiplier)


def infer_observed_debit(
    data: dict[str, Any],
    previous_balance_total: int | float | None,
) -> float | None:
    if previous_balance_total is None:
        return None
    current_total = balance_total(data)
    outcome = data.get("outcome")
    if current_total is None or not isinstance(outcome, dict):
        return None
    win = outcome.get("win")
    if not isinstance(win, (int, float)):
        return None
    return float(previous_balance_total) + float(win) - float(current_total)


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
    """Return only provider-level continuation commands with a known wire shape.

    Unknown server-advertised actions remain diagnostic coverage and are never
    executed merely because state == available_action.
    """
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
    # Prefer a provider-advertised direct state command when it is in
    # the safe vocabulary. This covers preselection_game while preserving
    # play_preselection_game as an observed alias on runtimes that advertise it.
    if state in SAFE_CONTINUATION_COMMANDS and state in action_names:
        return state

    command = CONTINUATION_BY_STATE.get(state, "")
    if command and command in SAFE_CONTINUATION_COMMANDS and command in action_names:
        return command
    return ""


def pending_flow_actions(data: dict[str, Any]) -> list[str]:
    actions = set(flow_available_actions(data))
    continuation = flow_continuation_command(data)
    handled = {"init", "spin"}
    handled.update(SAFE_CONTINUATION_COMMANDS)
    handled.update(CONTINUATION_BY_STATE)
    if continuation:
        handled.add(continuation)
    return sorted(actions - handled)


def provider_error_envelope(data: dict[str, Any]) -> Any | None:
    """Return a sanitized provider error envelope, if present."""
    if not isinstance(data, dict) or "errors" not in data:
        return None
    errors = data.get("errors")
    if errors in (None, [], {}, ""):
        return None
    return sanitize_options(errors)


def http_error_evidence(response: requests.Response | None) -> dict[str, Any]:
    """Capture bounded, sanitized HTTP failure evidence for protocol learning."""
    if response is None:
        return {"status": None}
    evidence: dict[str, Any] = {
        "status": int(getattr(response, "status_code", 0) or 0),
        "content_type": str(response.headers.get("Content-Type") or ""),
    }
    try:
        payload = response.json()
    except Exception:
        payload = None
    if payload is not None:
        evidence["json"] = sanitize_options(payload)
    else:
        evidence["text"] = sanitize_error_text(
            str(getattr(response, "text", "") or "")
        )[:4000]
    return evidence

def runtime_shape_summary(data: dict[str, Any]) -> dict[str, Any]:
    """Return a value-free structural map for unknown BGaming runtimes."""
    summary: dict[str, Any] = {
        "top_level_keys": sorted(str(key) for key in data.keys()),
        "top_level_types": {
            str(key): type(value).__name__
            for key, value in sorted(data.items(), key=lambda item: str(item[0]))
        },
    }
    for key in ("options", "flow", "game", "features", "balance"):
        value = data.get(key)
        if isinstance(value, dict):
            summary[f"{key}_keys"] = sorted(str(item) for item in value.keys())
    for key in ("available_actions", "available_commands"):
        value = data.get(key)
        if isinstance(value, list):
            summary[key] = sorted({str(item) for item in value})
    return summary


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
    expected_outcome_bet: int | float | None = None,
    variable_layout: bool = False,
    allow_observed_debit: bool = False,
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
    if command == "spin":
        expected_bet = (
            expected_outcome_bet
            if isinstance(expected_outcome_bet, (int, float))
            else requested_bet
        )
        if (
            not isinstance(actual_bet, (int, float))
            or abs(float(actual_bet) - float(expected_bet)) > 1e-9
        ):
            warnings.append(
                f"bet devuelta={actual_bet!r}, esperada={expected_bet!r}, "
                f"solicitada={requested_bet!r}"
            )
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
    if (
        previous_balance_total is not None
        and current_total is not None
        and isinstance(win, (int, float))
    ):
        if allow_observed_debit and expected_debit is None:
            observed_debit = infer_observed_debit(data, previous_balance_total)
            if observed_debit is not None and observed_debit < -1e-9:
                warnings.append(
                    f"balance implica débito negativo imposible={observed_debit:g}"
                )
        else:
            debit = (
                float(expected_debit)
                if isinstance(expected_debit, (int, float))
                else (0.0 if command == "freespin" else float(actual_bet or 0))
            )
            expected = float(previous_balance_total) - debit + float(win)
            if abs(float(current_total) - expected) > 1e-9:
                warnings.append(
                    f"balance inconsistente: actual={current_total}, esperado={expected}, "
                    f"debito={debit}"
                )

    return warnings

