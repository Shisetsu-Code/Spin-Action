from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


@dataclass(slots=True)
class LauncherConfig:
    launcher_url: str
    gamename: str
    operator: str
    server_url: str
    currency: str
    mode: str
    lang: str
    provider_script_urls: list[str] = field(default_factory=list)

    @property
    def origin(self) -> str:
        parsed = urlparse(self.launcher_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def init_session_url(self) -> str:
        return self.server_url.rstrip("/") + "/init-session/demo"

    @property
    def gameserver_url(self) -> str:
        return self.server_url.rstrip("/") + "/gameserver/demo"


@dataclass(slots=True)
class RubyPlayClientProfile:
    protocol_version: int | None = None
    math_version: int | None = None
    rtp: float | None = None
    wager: float | None = None
    buy_feature_type: str = ""
    buy_feature_multiplier: float | None = None
    # Tri-state capability learned from the active client bundle:
    #   True  -> client contract proves Buy Feature support;
    #   False -> client contract proves the session has no Buy Feature API;
    #   None  -> unresolved, keep conservative coverage-gap behavior.
    buy_feature_client_supported: bool | None = None
    actions: list[str] = field(default_factory=list)
    source_scripts: list[str] = field(default_factory=list)
    bundle_sha256: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> "RubyPlayClientProfile | None":
        if not isinstance(raw, dict):
            return None
        try:
            raw_capability = raw.get("buy_feature_client_supported")
            capability = (
                bool(raw_capability)
                if isinstance(raw_capability, bool)
                else None
            )
            return cls(
                protocol_version=(
                    int(raw["protocol_version"])
                    if raw.get("protocol_version") is not None
                    else None
                ),
                math_version=(
                    int(raw["math_version"])
                    if raw.get("math_version") is not None
                    else None
                ),
                rtp=(float(raw["rtp"]) if raw.get("rtp") is not None else None),
                wager=(float(raw["wager"]) if raw.get("wager") is not None else None),
                buy_feature_type=str(raw.get("buy_feature_type") or ""),
                buy_feature_multiplier=(
                    float(raw["buy_feature_multiplier"])
                    if raw.get("buy_feature_multiplier") is not None
                    else None
                ),
                buy_feature_client_supported=capability,
                actions=[str(x) for x in raw.get("actions", []) if str(x)],
                source_scripts=[str(x) for x in raw.get("source_scripts", []) if str(x)],
                bundle_sha256=str(raw.get("bundle_sha256") or ""),
                evidence=[str(x) for x in raw.get("evidence", []) if str(x)],
            )
        except (TypeError, ValueError, KeyError):
            return None


@dataclass(slots=True)
class BetPlan:
    allowed_bets: list[int | float]
    default_index: int
    default_bet: int | float
    currency: str
    subunit: int | None
    wager: float | None

    @property
    def effective_stake(self) -> float | None:
        if not isinstance(self.wager, (int, float)) or self.wager <= 0:
            return None
        return float(self.default_bet) * float(self.wager)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_bets": list(self.allowed_bets),
            "default_index": self.default_index,
            "default_bet": self.default_bet,
            "currency": self.currency,
            "subunit": self.subunit,
            "wager": self.wager,
            "effective_stake": self.effective_stake,
            "policy": "default",
        }


@dataclass(slots=True)
class RubyPlayRuntime:
    session: requests.Session
    launcher: LauncherConfig
    client_profile: RubyPlayClientProfile
    session_key: str
    fun_mode_data: dict[str, Any]
    init_data: dict[str, Any]
    bets: list[int | float]
    default_bet: int | float
    default_bet_index: int
    currency: str
    subunit: int | None
    action_number: int
    next_action: str
    active_feature_type: str = ""

    @property
    def gameserver_url(self) -> str:
        return self.launcher.gameserver_url

    @property
    def bet_plan(self) -> BetPlan:
        return BetPlan(
            allowed_bets=list(self.bets),
            default_index=self.default_bet_index,
            default_bet=self.default_bet,
            currency=self.currency,
            subunit=self.subunit,
            wager=self.client_profile.wager,
        )


def _unique_numeric(values: Iterable[str | int | float], cast=float):
    clean = []
    for value in values:
        try:
            number = cast(value)
        except (TypeError, ValueError):
            continue
        if number not in clean:
            clean.append(number)
    return clean[0] if len(clean) == 1 else None


def extract_launcher_url(public_html: str, public_url: str) -> str:
    soup = BeautifulSoup(public_html or "", "html.parser")
    candidates: list[str] = []
    for iframe in soup.find_all("iframe"):
        raw = str(iframe.get("src") or "").strip()
        if not raw:
            continue
        absolute = urljoin(public_url, raw)
        parsed = urlparse(absolute)
        params = parse_qs(parsed.query)
        if parsed.path.rstrip("/").endswith("/launcher") and {
            "gamename",
            "server_url",
        }.issubset(params):
            candidates.append(absolute)
    unique = list(dict.fromkeys(candidates))
    if len(unique) != 1:
        raise ValueError(
            f"RubyPlay: launcher no unívoco en ficha pública (candidatos={len(unique)})."
        )
    return unique[0]


def parse_launcher_url(launcher_url: str) -> LauncherConfig:
    parsed = urlparse(launcher_url)
    params = parse_qs(parsed.query, keep_blank_values=True)

    def one(name: str, *, required: bool = True) -> str:
        values = [str(x) for x in params.get(name, [])]
        if len(values) != 1 or (required and not values[0]):
            raise ValueError(f"RubyPlay launcher: parámetro {name!r} no unívoco.")
        return values[0] if values else ""

    config = LauncherConfig(
        launcher_url=launcher_url,
        gamename=one("gamename"),
        operator=one("operator"),
        server_url=one("server_url"),
        currency=one("currency"),
        mode=one("mode"),
        lang=one("lang", required=False) or "en",
    )
    server = urlparse(config.server_url)
    if server.scheme not in {"http", "https"} or not server.netloc:
        raise ValueError("RubyPlay launcher: server_url inválido.")
    return config


def _rp_config_value(launcher_html: str, key: str) -> str:
    block = re.search(
        r"(?:window\.)?RP_CONFIG\s*=\s*\{(?P<body>.*?)\}\s*;",
        launcher_html or "",
        re.S,
    )
    if not block:
        return ""
    match = re.search(
        rf"\b{re.escape(key)}\s*:\s*(['\"])(.*?)\1",
        block.group("body"),
        re.S,
    )
    return str(match.group(2)).strip() if match else ""


def extract_provider_script_urls(launcher_html: str, launcher_url: str) -> list[str]:
    soup = BeautifulSoup(launcher_html or "", "html.parser")
    allowed_hosts = {urlparse(launcher_url).netloc.lower()}
    for key in ("cdn", "root"):
        value = _rp_config_value(launcher_html, key)
        if value:
            host = urlparse(value).netloc.lower()
            if host:
                allowed_hosts.add(host)

    urls: list[str] = []
    for script in soup.find_all("script"):
        raw = str(script.get("src") or "").strip()
        if not raw:
            continue
        absolute = urljoin(launcher_url, raw)
        if urlparse(absolute).netloc.lower() not in allowed_hosts:
            continue
        urls.append(absolute)
    return list(dict.fromkeys(urls))


def _action_wrapper_map(bundle: str) -> dict[int, str]:
    by_index: dict[int, set[str]] = {}
    for raw_index, name in re.findall(
        r"new\s+[A-Za-z_$][A-Za-z0-9_$]*"
        r"\((\d+),['\"][^'\"]+['\"],['\"]([^'\"]+)['\"],",
        bundle or "",
    ):
        by_index.setdefault(int(raw_index), set()).add(str(name))
    return {
        index: next(iter(names))
        for index, names in by_index.items()
        if len(names) == 1
    }


def discover_client_profile(
    scripts: list[tuple[str, str]],
) -> RubyPlayClientProfile:
    contract_parts = [
        (url, text)
        for url, text in scripts
        if text
        and (
            "com.gongxigames.math" in text
            or "v_protocol" in text
            or "MATH_VERSION" in text
        )
    ]
    bundle = "\n".join(text for _url, text in contract_parts)
    profile = RubyPlayClientProfile(
        source_scripts=[url for url, _text in contract_parts],
        bundle_sha256=(
            hashlib.sha256(bundle.encode("utf-8", errors="replace")).hexdigest()
            if bundle
            else ""
        ),
    )
    if not bundle:
        return profile

    protocol_candidates = re.findall(
        r"([A-Za-z_$][A-Za-z0-9_$]*)\.VERSION\s*=\s*(\d+)"
        r".{0,500}?__class=['\"]com\.gongxigames\.math\.core\.binary\.BinarySerializer['\"]",
        bundle,
        re.S,
    )
    protocol = _unique_numeric((value for _alias, value in protocol_candidates), int)
    if protocol is not None:
        profile.protocol_version = int(protocol)
        profile.evidence.append("client.BinarySerializer.VERSION")

    math_bases = re.findall(
        r"MATH_VERSION\s*=\s*(\d+)\s*\+\s*[A-Za-z_$][A-Za-z0-9_$]*\.RTP",
        bundle,
    )
    rtps = re.findall(r"\.RTP\s*=\s*(\d+(?:\.\d+)?)", bundle)
    math_base = _unique_numeric(math_bases, int)
    rtp = _unique_numeric(rtps, float)
    if rtp is not None:
        profile.rtp = float(rtp)
        profile.evidence.append("client.engine.RTP")
    if math_base is not None and rtp is not None and float(rtp).is_integer():
        profile.math_version = int(math_base) + int(rtp)
        profile.evidence.append("client.engine.MATH_VERSION=base+RTP")
    else:
        direct_math = _unique_numeric(
            re.findall(r"\.MATH_VERSION\s*=\s*(\d{6,})", bundle),
            int,
        )
        if direct_math is not None:
            profile.math_version = int(direct_math)
            profile.evidence.append("client.engine.MATH_VERSION")

    wager = _unique_numeric(
        re.findall(r"\.WAGER\s*=\s*(\d+(?:\.\d+)?)", bundle),
        float,
    )
    if wager is not None and wager > 0:
        profile.wager = float(wager)
        profile.evidence.append("client.engine.WAGER")

    action_map = _action_wrapper_map(bundle)
    profile.actions = [action_map[index] for index in sorted(action_map)]
    if action_map:
        profile.evidence.append("client.Action._$wrappers")

    buy_type_candidates: list[str] = []
    for match in re.finditer(
        r"getBuyFeatureType\([^)]*\)\{(.{0,1600}?)\}isBuyFeatureGame",
        bundle,
        re.S,
    ):
        indexes = {
            int(raw)
            for raw in re.findall(
                r"_\$wrappers\[(\d+)\]\.getName\(\)",
                match.group(1),
            )
        }
        for index in indexes:
            name = action_map.get(index)
            if name and name not in buy_type_candidates:
                buy_type_candidates.append(name)
    if len(buy_type_candidates) == 1:
        profile.buy_feature_type = buy_type_candidates[0]
        profile.evidence.append("client.getBuyFeatureType->Action wrapper")

    buy_multiplier = _unique_numeric(
        re.findall(
            r"BUY_FEATURE_[A-Z0-9_]*COST_IN_TIMES_BET\s*=\s*(\d+(?:\.\d+)?)",
            bundle,
        ),
        float,
    )
    if buy_multiplier is not None and buy_multiplier > 0:
        profile.buy_feature_multiplier = float(buy_multiplier)
        profile.evidence.append("client.BUY_FEATURE_*_COST_IN_TIMES_BET")

    return profile


def bet_plan_from_init(
    init_data: dict[str, Any],
    *,
    wager: float | None,
) -> BetPlan:
    data = init_data.get("data")
    if not isinstance(data, dict):
        raise ValueError("RubyPlay init: falta data.")
    game_config = data.get("game_config")
    if not isinstance(game_config, dict):
        raise ValueError("RubyPlay init: falta data.game_config.")
    raw_bets = game_config.get("bets")
    if not isinstance(raw_bets, list):
        raise ValueError("RubyPlay init: game_config.bets no es lista.")
    bets = [
        value
        for value in raw_bets
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    ]
    if not bets or len(bets) != len(raw_bets):
        raise ValueError("RubyPlay init: lista de apuestas inválida.")
    try:
        default_index = int(game_config.get("def_bet_index"))
    except (TypeError, ValueError):
        default_index = -1
    if default_index < 0 or default_index >= len(bets):
        raise ValueError("RubyPlay init: def_bet_index fuera de rango.")

    player = data.get("player")
    if not isinstance(player, dict):
        player = {}
    currency = str(player.get("currency") or "")
    try:
        subunit = int(player.get("subunit"))
        if subunit <= 0:
            subunit = None
    except (TypeError, ValueError):
        subunit = None

    return BetPlan(
        allowed_bets=bets,
        default_index=default_index,
        default_bet=bets[default_index],
        currency=currency,
        subunit=subunit,
        wager=wager,
    )


def _write_json(path: Path | None, payload: Any) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_text(path: Path | None, text: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def sanitize_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(payload))
    if "key" in out:
        out["key"] = "<redacted-session-key>"
    return out


def _load_json_response(response: requests.Response, label: str) -> dict[str, Any]:
    response.raise_for_status()
    try:
        payload = response.json()
    except Exception as exc:
        raise ValueError(f"RubyPlay {label}: respuesta no JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"RubyPlay {label}: respuesta JSON no es objeto.")
    return payload


def bootstrap_game(
    session: requests.Session,
    public_url: str,
    *,
    timeout_s: float,
    cached_profile: RubyPlayClientProfile | None = None,
    artifact_dir: Path | None = None,
) -> RubyPlayRuntime:
    public = session.get(public_url, timeout=timeout_s, allow_redirects=True)
    public.raise_for_status()
    public_html = public.text
    _write_text(artifact_dir / "public.html" if artifact_dir else None, public_html)

    launcher_url = extract_launcher_url(public_html, public.url or public_url)
    launcher = parse_launcher_url(launcher_url)

    launcher_response = session.get(launcher_url, timeout=timeout_s, allow_redirects=True)
    launcher_response.raise_for_status()
    launcher_html = launcher_response.text
    _write_text(artifact_dir / "launcher.html" if artifact_dir else None, launcher_html)

    launcher.provider_script_urls = extract_provider_script_urls(
        launcher_html,
        launcher_response.url or launcher_url,
    )

    profile = cached_profile
    if profile is None:
        scripts: list[tuple[str, str]] = []
        for url in launcher.provider_script_urls:
            response = session.get(url, timeout=timeout_s)
            response.raise_for_status()
            scripts.append((url, response.text))
        profile = discover_client_profile(scripts)
    if profile.protocol_version is None:
        raise ValueError("RubyPlay: no se pudo descubrir v_protocol desde el cliente.")
    if profile.math_version is None:
        raise ValueError("RubyPlay: no se pudo descubrir v_math desde el cliente.")

    session.headers.update(
        {
            "Origin": launcher.origin,
            "Referer": launcher.origin + "/",
        }
    )
    init_session = session.get(
        launcher.init_session_url,
        params={
            "currency": launcher.currency,
            "gamename": launcher.gamename,
            "mode": launcher.mode,
            "operator": launcher.operator,
            "playerSession": "",
        },
        timeout=timeout_s,
    )
    init_session_payload = _load_json_response(init_session, "init-session")
    _write_json(
        artifact_dir / "init-session-response.json" if artifact_dir else None,
        {
            **init_session_payload,
            "sessionKey": "<redacted-session-key>"
            if init_session_payload.get("sessionKey")
            else "",
        },
    )
    if str(init_session_payload.get("status") or "").lower() != "success":
        raise ValueError(
            f"RubyPlay init-session: status={init_session_payload.get('status')!r}."
        )
    session_key = str(init_session_payload.get("sessionKey") or "")
    fun_mode_data = init_session_payload.get("funModeData")
    if not session_key or not isinstance(fun_mode_data, dict):
        raise ValueError("RubyPlay init-session: faltan sessionKey/funModeData.")

    init_request = {
        "v_protocol": profile.protocol_version,
        "v_math": profile.math_version,
        "key": session_key,
        "device_type": "desktop",
        "funModeData": dict(fun_mode_data),
        "action": "init",
    }
    init_response = session.post(
        launcher.gameserver_url,
        json=init_request,
        timeout=timeout_s,
    )
    init_data = _load_json_response(init_response, "gameserver/init")
    _write_json(
        artifact_dir / "init-request.json" if artifact_dir else None,
        sanitize_request_payload(init_request),
    )
    _write_json(
        artifact_dir / "init-response.json" if artifact_dir else None,
        init_data,
    )
    if str(init_data.get("status") or "").lower() != "ok":
        raise ValueError(f"RubyPlay init: status={init_data.get('status')!r}.")
    if str(init_data.get("topic") or "") != "gameserver/init":
        raise ValueError(f"RubyPlay init: topic inesperado={init_data.get('topic')!r}.")

    plan = bet_plan_from_init(init_data, wager=profile.wager)
    data = init_data.get("data") if isinstance(init_data.get("data"), dict) else {}
    response_fun_mode_data = init_data.get("funModeData")
    if isinstance(response_fun_mode_data, dict):
        fun_mode_data = dict(response_fun_mode_data)
    try:
        action_number = int(data.get("an"))
    except (TypeError, ValueError):
        raise ValueError("RubyPlay init: data.an inválido.")
    next_action = str(data.get("next_action") or "")
    if not next_action:
        raise ValueError("RubyPlay init: data.next_action vacío.")

    return RubyPlayRuntime(
        session=session,
        launcher=launcher,
        client_profile=profile,
        session_key=session_key,
        fun_mode_data=fun_mode_data,
        init_data=init_data,
        bets=list(plan.allowed_bets),
        default_bet=plan.default_bet,
        default_bet_index=plan.default_index,
        currency=plan.currency,
        subunit=plan.subunit,
        action_number=action_number,
        next_action=next_action,
    )


def purchase_price(
    bet: int | float,
    *,
    wager: float | None,
    multiplier: float | None,
) -> int | float | None:
    if not isinstance(wager, (int, float)) or wager <= 0:
        return None
    if not isinstance(multiplier, (int, float)) or multiplier <= 0:
        return None
    value = float(bet) * float(wager) * float(multiplier)
    rounded = round(value)
    return int(rounded) if math.isclose(value, rounded, abs_tol=1e-9) else value


def post_action(
    runtime: RubyPlayRuntime,
    action: str,
    *,
    timeout_s: float,
    bet: int | float | None = None,
    buy_feature_type: str = "",
    buy_feature_price: int | float | None = None,
) -> tuple[requests.Response, dict[str, Any], dict[str, Any], int]:
    command = str(action or "").strip().lower()
    if not command or command == "init":
        raise ValueError("RubyPlay: post_action requiere una acción posterior a init.")

    previous_an = runtime.action_number
    payload: dict[str, Any] = {
        "v_protocol": runtime.client_profile.protocol_version,
        "v_math": runtime.client_profile.math_version,
        "an": previous_an,
        "bets": list(runtime.bets),
        "key": runtime.session_key,
        "device_type": "desktop",
        "funModeData": dict(runtime.fun_mode_data),
        "action": command,
    }

    if command in {"spin", "buy_feature"}:
        if not isinstance(bet, (int, float)) or isinstance(bet, bool) or bet <= 0:
            raise ValueError(f"RubyPlay {command}: bet requerido.")
        if bet not in runtime.bets:
            raise ValueError(f"RubyPlay {command}: bet no anunciado por init: {bet!r}.")
        payload["bet"] = bet

    feature_type = str(buy_feature_type or runtime.active_feature_type or "").strip().lower()
    if command == "buy_feature":
        if not feature_type:
            raise ValueError("RubyPlay buy_feature: tipo no descubierto.")
        if (
            not isinstance(buy_feature_price, (int, float))
            or isinstance(buy_feature_price, bool)
            or buy_feature_price <= 0
        ):
            raise ValueError("RubyPlay buy_feature: precio no descubierto.")
        payload["buy_feature_type"] = feature_type
        payload["buy_feature_price"] = buy_feature_price
    elif command in {"respin", "freespin"} and feature_type:
        payload["buy_feature_type"] = feature_type

    response = runtime.session.post(
        runtime.gameserver_url,
        json=payload,
        timeout=timeout_s,
    )
    data = _load_json_response(response, f"gameserver/{command}")
    if str(data.get("status") or "").lower() != "ok":
        raise ValueError(f"RubyPlay {command}: status={data.get('status')!r}.")

    body = data.get("data")
    if not isinstance(body, dict):
        raise ValueError(f"RubyPlay {command}: falta data.")
    try:
        new_an = int(body.get("an"))
    except (TypeError, ValueError):
        raise ValueError(f"RubyPlay {command}: data.an inválido.")
    next_action = str(body.get("next_action") or "").strip().lower()
    if not next_action:
        raise ValueError(f"RubyPlay {command}: data.next_action vacío.")

    response_fun_mode_data = data.get("funModeData")
    if isinstance(response_fun_mode_data, dict):
        runtime.fun_mode_data = dict(response_fun_mode_data)
    runtime.action_number = new_an
    runtime.next_action = next_action

    response_feature_type = str(body.get("buy_feature_type") or "").strip().lower()
    if response_feature_type:
        runtime.active_feature_type = response_feature_type
    elif command == "buy_feature" and feature_type:
        runtime.active_feature_type = feature_type
    if next_action == "spin":
        runtime.active_feature_type = ""

    return response, payload, data, previous_an


def validate_action_response(
    data: dict[str, Any],
    *,
    action: str,
    previous_an: int,
) -> list[str]:
    warnings: list[str] = []
    if str(data.get("topic") or "") != f"gameserver/{action}":
        warnings.append(f"topic={data.get('topic')!r}")
    body = data.get("data")
    if not isinstance(body, dict):
        warnings.append("data ausente")
        return warnings
    try:
        new_an = int(body.get("an"))
        if new_an != previous_an + 1:
            warnings.append(f"an {previous_an}->{new_an}, esperado +1")
    except (TypeError, ValueError):
        warnings.append("data.an inválido")
    if not str(body.get("next_action") or ""):
        warnings.append("next_action vacío")
    return warnings
