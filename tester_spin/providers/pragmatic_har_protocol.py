from __future__ import annotations

from pathlib import Path
from typing import Any

from tester_spin.providers.pragmatic_protocol import (
    analyze_response as _base_analyze_response,
    summarize_analysis_files as _base_summarize_analysis_files,
)


HAR_AUTOMATED_STATE_KINDS = {
    "free_spin_option_required": "doFSOption",
    "mystery_feature_step_required": "doMysteryScatter",
}


def analyze_response(fields: dict[str, str]) -> dict[str, Any]:
    """Augment the generic analyzer with transitions proven by real HAR captures."""
    analysis = _base_analyze_response(fields)
    state = str(analysis.get("state_kind") or "")
    if state == "free_spin_option_required" and fields.get("fs_opt"):
        analysis["automatic_handler"] = "doFSOption"
        analysis["handler_evidence"] = "HAR: na=fso -> doFSOption(ind)"
    elif state == "mystery_feature_step_required" and (fields.get("mb") or fields.get("psym")):
        analysis["automatic_handler"] = "doMysteryScatter"
        analysis["handler_evidence"] = "HAR: na=m -> doMysteryScatter"
    return analysis


def summarize_analysis_files(run_root: Path) -> dict[str, Any]:
    summary = _base_summarize_analysis_files(run_root)
    examples = []
    for item in summary.get("unhandled_signatures") or summary.get("unknown_signatures") or []:
        state = str(item.get("state_kind") or "")
        if state in HAR_AUTOMATED_STATE_KINDS:
            continue
        examples.append(item)
    summary["unknown_signatures"] = examples
    summary["unhandled_signatures"] = examples
    summary["har_automated_states"] = dict(HAR_AUTOMATED_STATE_KINDS)
    return summary
