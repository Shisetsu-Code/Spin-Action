from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult, SpinAttempt, utc_now_iso
from tester_spin.providers.base import Progress
from tester_spin.providers.redtiger.evolution_launch import bootstrap_game
from tester_spin.providers.redtiger.runtime import (
    ChoicePrompt,
    FeatureBuy,
    RedTigerRuntime,
    apply_response_token,
    build_choice_payload,
    build_spin_payload,
    choice_url_from_spin_url,
    pending_choice_from_response,
    response_summary,
    sanitize_payload,
    validate_spin_response,
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _mode_id(value: str) -> str:
    clean = "".join(ch if ch.isalnum() else "_" for ch in str(value).upper()).strip("_")
    return clean or "UNKNOWN"


def _sanitize_direct_http_headers(runtime: RedTigerRuntime) -> None:
    browser_only = {
        "connection",
        "content-length",
        "cookie",
        "host",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
    for key in list(runtime.session.headers):
        name = str(key or "")
        lowered = name.casefold()
        if (
            not name
            or name.startswith(":")
            or lowered in browser_only
            or any(ch in name for ch in "\r\n\t ")
        ):
            runtime.session.headers.pop(key, None)
