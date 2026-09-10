from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


# BGaming demo/FUN endpoints expose monetary amounts in minor units.  The UI
# displays two decimal places (100000 backend units == 1000.00 FUN).  Protocol
# calculations intentionally stay in raw units; this module is presentation only.
_FUN_SCALE = Decimal("100")
_AMOUNT = re.compile(
    r"(?P<prefix>\b(?:balance_total|balance|bet|debit|win|line_bet|win_inferido)=)"
    r"(?P<value>-?\d+(?:\.\d+)?)"
    r"(?!\s*FUN\b)",
    re.IGNORECASE,
)


def _human_amount(raw_text: str) -> str:
    try:
        value = Decimal(raw_text) / _FUN_SCALE
    except (InvalidOperation, ValueError):
        return raw_text
    return f"{value:.2f} FUN (raw={raw_text})"


def humanize_bgaming_progress(message: str) -> str:
    """Render backend minor units as FUN while retaining their raw value.

    This changes only user-facing progress strings.  Saved protocol artifacts,
    validation, balance deltas and multiplier calculations keep the exact raw
    values returned by BGaming.
    """
    text = str(message or "")

    def replace(match: re.Match[str]) -> str:
        return match.group("prefix") + _human_amount(match.group("value"))

    return _AMOUNT.sub(replace, text)


__all__ = ["humanize_bgaming_progress"]
