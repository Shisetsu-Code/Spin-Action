from __future__ import annotations

import re
import threading
from decimal import Decimal, InvalidOperation


# BGaming demo/FUN endpoints expose monetary amounts in minor units. The UI
# displays two decimal places (100000 backend units == 1000.00 FUN). Protocol
# calculations intentionally stay in raw units; this module is presentation only.
_FUN_SCALE = Decimal("100")
_AMOUNT = re.compile(
    r"(?P<prefix>\b(?:balance_total|balance|bet|debit|win|line_bet|win_inferido)=)"
    r"(?P<value>-?\d+(?:\.\d+)?)"
    r"(?![\d.]|\s*FUN\b)",
    re.IGNORECASE,
)
_install_lock = threading.Lock()
_installed = False


def _human_amount(raw_text: str) -> str:
    try:
        value = Decimal(raw_text) / _FUN_SCALE
    except (InvalidOperation, ValueError):
        return raw_text
    return f"{value:.2f} FUN (raw={raw_text})"


def humanize_bgaming_progress(message: str) -> str:
    """Render backend minor units as FUN while retaining their raw value.

    This changes only user-facing progress strings. Saved protocol artifacts,
    validation, balance deltas and multiplier calculations keep the exact raw
    values returned by BGaming.
    """
    text = str(message or "")
    if " FUN (raw=" in text:
        return text

    def replace(match: re.Match[str]) -> str:
        return match.group("prefix") + _human_amount(match.group("value"))

    return _AMOUNT.sub(replace, text)


def install_money_display_adapter() -> None:
    """Normalize only progress output at the BGaming provider boundary."""
    global _installed
    with _install_lock:
        if _installed:
            return

        from tester_spin.providers.bgaming.adapter import BGamingProvider as BaseBGamingProvider

        original_test_game = BaseBGamingProvider.test_game

        def money_display_test_game(self, game, **kwargs):
            raw_progress = kwargs.get("progress")
            if callable(raw_progress):
                def display_progress(message: str) -> None:
                    raw_progress(humanize_bgaming_progress(str(message)))

                kwargs["progress"] = display_progress
            return original_test_game(self, game, **kwargs)

        BaseBGamingProvider.test_game = money_display_test_game
        _installed = True


__all__ = ["humanize_bgaming_progress", "install_money_display_adapter"]
