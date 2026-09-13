from __future__ import annotations

from typing import Any

import tester_spin.providers.bgaming_path_policy as _policy
import tester_spin.providers.bgaming_paths_v2 as _paths
from tester_spin.providers.bgaming import execution as _execution
from tester_spin.providers.bgaming.server_guided import (
    _load_client_bundle,
    discover_server_guided_client_evidence,
    remember_dynamic_evidence,
)
from tester_spin.providers.bgaming.ui_index_domain import (
    augment_evidence_with_ui_index_domain,
)


_INSTALLED = False
_ORIGINAL_POST_COMMAND = None


def _replace_artifact_evidence(
    original: dict[str, Any],
    enriched: dict[str, Any],
) -> None:
    if original == enriched:
        return
    items = getattr(_policy._LOCAL, "server_guided_evidence", None)
    seen = getattr(_policy._LOCAL, "server_guided_seen", None)
    if not isinstance(items, list) or not isinstance(seen, set):
        return

    old_key = _paths._evidence_key(original)
    new_key = _paths._evidence_key(enriched)
    items[:] = [
        item
        for item in items
        if not isinstance(item, dict) or _paths._evidence_key(item) != old_key
    ]
    seen.discard(old_key)
    if new_key not in seen:
        seen.add(new_key)
        items.append(enriched)


def _record_error(exc: Exception) -> None:
    diagnostics = getattr(_policy._LOCAL, "server_guided_errors", None)
    if not isinstance(diagnostics, list):
        diagnostics = []
        _policy._LOCAL.server_guided_errors = diagnostics
    message = f"ui-index: {type(exc).__name__}: {exc}"
    if message not in diagnostics:
        diagnostics.append(message[:800])


def _post_command_with_ui_index_domain(
    runtime,
    command: str,
    *,
    timeout_s: float,
    options: dict[str, Any] | None = None,
    extra_data: dict[str, Any] | None = None,
):
    assert _ORIGINAL_POST_COMMAND is not None
    result = _ORIGINAL_POST_COMMAND(
        runtime,
        command,
        timeout_s=timeout_s,
        options=options,
        extra_data=extra_data,
    )
    if not _paths._coverage_active():
        return result

    try:
        data = result[2]
        original = discover_server_guided_client_evidence(
            runtime,
            data,
            timeout_s=timeout_s,
        )
        if not isinstance(original, dict):
            return result
        bundle, _source = _load_client_bundle(runtime, timeout_s=timeout_s)
        enriched = augment_evidence_with_ui_index_domain(
            data,
            original,
            bundle,
        )
        if enriched != original:
            remember_dynamic_evidence(enriched)
            _replace_artifact_evidence(original, enriched)
    except Exception as exc:
        _record_error(exc)
    return result


def install_bgaming_ui_index_bridge() -> None:
    """Install the provider-local scene-graph index resolver once."""
    global _INSTALLED, _ORIGINAL_POST_COMMAND
    if _INSTALLED:
        return
    if getattr(_execution.post_command, "__name__", "") == "_post_command_with_ui_index_domain":
        _INSTALLED = True
        return
    _ORIGINAL_POST_COMMAND = _execution.post_command
    _execution.post_command = _post_command_with_ui_index_domain
    _INSTALLED = True


__all__ = ["install_bgaming_ui_index_bridge"]
