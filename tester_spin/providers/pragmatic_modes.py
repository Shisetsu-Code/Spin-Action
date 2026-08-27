from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any


def _num(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _float_list(raw: str | None) -> list[float]:
    out: list[float] = []
    for part in str(raw or "").split(","):
        value = _num(part)
        if value is not None:
            out.append(value)
    return out


def _first_positive(mapping: dict[str, str], keys: tuple[str, ...]) -> tuple[float | None, str]:
    for key in keys:
        value = _num(mapping.get(key))
        if value is not None and value > 0:
            return value, key
    return None, ""


def _enabled_tokens(raw: str | None) -> list[bool]:
    if raw is None or str(raw).strip() == "":
        return []
    values: list[bool] = []
    for part in re.split(r"[,;|~ ]+", str(raw).strip()):
        if not part:
            continue
        values.append(part.strip().lower() not in {"0", "0.0", "false", "off", "no", "null", "none"})
    return values


def _purchase_bets(raw: str | None) -> list[float]:
    text = str(raw or "")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            values: list[float] = []
            for item in parsed:
                if isinstance(item, dict) and "bet" in item:
                    number = _num(item.get("bet"))
                    if number is not None:
                        values.append(number)
            if values:
                return values
    except Exception:
        pass

    values = [
        float(x)
        for x in re.findall(
            r"[\"']?bet[\"']?\s*:\s*([0-9]+(?:\.[0-9]+)?)",
            text,
            flags=re.I,
        )
    ]
    if values:
        return values

    if re.fullmatch(r"\s*[0-9]+(?:\.[0-9]+)?(?:\s*,\s*[0-9]+(?:\.[0-9]+)?)*\s*", text):
        return [float(x.strip()) for x in text.split(",") if x.strip()]
    return []


@dataclass(frozen=True, slots=True)
class PragmaticMode:
    id: str
    kind: str
    ordinal: int
    provider_bl: int | None = None
    provider_pur: int | None = None
    enabled: bool = True
    price_x_base: float = 1.0
    paid_cost: float = 0.0
    price_known: bool = True
    source_field: str = ""
    source_value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PragmaticModeCatalog:
    base_scale: float
    base_coin: float
    base_bet: float
    modes: tuple[PragmaticMode, ...]
    raw_bls: str
    raw_pur_init: str
    raw_pur_init_e: str
    mode_evidence: dict[str, str]

    def enabled(self) -> list[PragmaticMode]:
        return [mode for mode in self.modes if mode.enabled]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "tester-spin/pragmatic-mode-catalog/v2",
            "base_scale": self.base_scale,
            "base_coin": self.base_coin,
            "base_bet": self.base_bet,
            "raw_sources": {
                "bls": self.raw_bls,
                "purInit": self.raw_pur_init,
                "purInit_e": self.raw_pur_init_e,
            },
            "mode_evidence": self.mode_evidence,
            "modes": [mode.to_dict() for mode in self.modes],
        }


def discover_modes(init: dict[str, str], requested_base_bet: float = 2.0) -> PragmaticModeCatalog:
    raw_bls = str(init.get("bls") or "")
    raw_pur_init = str(init.get("purInit") or "")
    raw_pur_init_e = str(init.get("purInit_e") or "")

    bls = _float_list(raw_bls)
    scale_source = ""
    if bls:
        base_scale = bls[0]
        scale_source = "bls[0]"
    else:
        base_scale, scale_source = _first_positive(
            init,
            ("l", "defl", "def_l", "lines", "lineCount", "line_count", "baseScale", "base_scale"),
        )
        if base_scale is None:
            coin_probe, _ = _first_positive(init, ("c", "defc", "def_c", "coin", "defaultCoin", "default_coin"))
            if coin_probe is not None or _float_list(init.get("sc")):
                base_scale = 1.0
                scale_source = "unit-scale-fallback"
            else:
                interesting = sorted(
                    key for key in init
                    if any(token in key.lower() for token in ("bet", "coin", "line", "scale", "stake", "wager", "bl"))
                )
                raise ValueError(
                    "doInit no contiene bls ni una escala de apuesta alternativa utilizable; "
                    f"campos relacionados={interesting[:30]}"
                )

    allowed_coins = _float_list(init.get("sc"))
    desired_coin = requested_base_bet / base_scale
    explicit_coin, coin_source = _first_positive(
        init,
        ("c", "defc", "def_c", "coin", "defaultCoin", "default_coin"),
    )

    if bls:
        if allowed_coins:
            base_coin = desired_coin if any(
                math.isclose(desired_coin, coin, rel_tol=0.0, abs_tol=1e-9)
                for coin in allowed_coins
            ) else min(allowed_coins)
            coin_source = "requested/sc" if any(
                math.isclose(desired_coin, coin, rel_tol=0.0, abs_tol=1e-9)
                for coin in allowed_coins
            ) else "sc[0]"
        else:
            base_coin = desired_coin
            coin_source = "requested-base-bet"
    else:
        if explicit_coin is not None:
            base_coin = explicit_coin
        elif allowed_coins:
            base_coin = min(allowed_coins, key=lambda coin: abs(coin - desired_coin))
            coin_source = "nearest-sc"
        else:
            base_coin = desired_coin
            coin_source = "requested-base-bet"

    base_bet = base_coin * base_scale

    modes: list[PragmaticMode] = [
        PragmaticMode(
            id="SPIN",
            kind="SPIN",
            ordinal=0,
            provider_bl=0,
            enabled=True,
            price_x_base=1.0,
            paid_cost=base_bet,
            source_field=scale_source,
            source_value=str(base_scale),
        )
    ]

    for provider_bl, scale in enumerate(bls[1:], start=1):
        price_x = scale / base_scale
        modes.append(
            PragmaticMode(
                id=f"ANTE_BET_{provider_bl}",
                kind="ANTE_BET",
                ordinal=provider_bl,
                provider_bl=provider_bl,
                enabled=True,
                price_x_base=price_x,
                paid_cost=base_bet * price_x,
                source_field=f"bls[{provider_bl}]",
                source_value=str(scale),
            )
        )

    purchase_bets = _purchase_bets(raw_pur_init)
    enabled_tokens = _enabled_tokens(raw_pur_init_e)
    purchase_count = max(len(purchase_bets), len(enabled_tokens))
    if purchase_count:
        if not enabled_tokens:
            enabled_tokens = [True] * purchase_count
        elif len(enabled_tokens) < purchase_count:
            enabled_tokens.extend([True] * (purchase_count - len(enabled_tokens)))

    for provider_pur in range(purchase_count):
        ordinal = provider_pur + 1
        purchase_bet = purchase_bets[provider_pur] if provider_pur < len(purchase_bets) else None
        price_known = purchase_bet is not None
        price_x = (purchase_bet / base_scale) if purchase_bet is not None else 0.0
        modes.append(
            PragmaticMode(
                id=f"PURCHASE_{ordinal}",
                kind="PURCHASE",
                ordinal=ordinal,
                provider_bl=0,
                provider_pur=provider_pur,
                enabled=enabled_tokens[provider_pur],
                price_x_base=price_x,
                paid_cost=base_bet * price_x,
                price_known=price_known,
                source_field=f"purInit[{provider_pur}]",
                source_value="" if purchase_bet is None else str(purchase_bet),
            )
        )

    evidence: dict[str, str] = {
        "base_scale_source": scale_source,
        "base_coin_source": coin_source,
    }
    for key, value in init.items():
        lowered = key.lower()
        if any(
            token in lowered
            for token in ("bet", "buy", "pur", "ante", "mode", "feature", "bonus", "bls", "coin", "line", "scale", "stake", "wager")
        ):
            evidence[str(key)] = str(value)

    return PragmaticModeCatalog(
        base_scale=base_scale,
        base_coin=base_coin,
        base_bet=base_bet,
        modes=tuple(modes),
        raw_bls=raw_bls,
        raw_pur_init=raw_pur_init,
        raw_pur_init_e=raw_pur_init_e,
        mode_evidence=evidence,
    )
