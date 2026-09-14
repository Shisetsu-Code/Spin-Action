from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable


_LOCAL = threading.local()


def active(provider_key: str) -> bool:
    return str(getattr(_LOCAL, "provider_key", "")) == str(provider_key)


def reserve(count: int = 1) -> None:
    callback = getattr(_LOCAL, "request_slot", None)
    if not callable(callback):
        return
    for _ in range(max(0, int(count))):
        if not bool(callback()):
            raise InterruptedError("provider request cancelled by shared rate limiter")


@contextmanager
def scope(provider_key: str, request_slot: Callable[[], bool] | None):
    old_key = getattr(_LOCAL, "provider_key", None)
    old_slot = getattr(_LOCAL, "request_slot", None)
    _LOCAL.provider_key = str(provider_key)
    _LOCAL.request_slot = request_slot
    try:
        yield
    finally:
        if old_key is None:
            try:
                delattr(_LOCAL, "provider_key")
            except AttributeError:
                pass
        else:
            _LOCAL.provider_key = old_key
        if old_slot is None:
            try:
                delattr(_LOCAL, "request_slot")
            except AttributeError:
                pass
        else:
            _LOCAL.request_slot = old_slot


__all__ = ["active", "reserve", "scope"]
