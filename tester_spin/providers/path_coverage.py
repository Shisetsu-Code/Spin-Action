from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tester_spin.models import GameTestResult


_EMPTY_SELECTIONS = {None, ""}


def _clean_option(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def _option_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    found: list[str] = []
    for item in value:
        clean = _clean_option(item)
        if clean and clean not in found:
            found.append(clean)
    return found


def _mode_scope(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return ""
    if len(relative.parts) < 2:
        return ""
    first = str(relative.parts[0])
    if first.casefold() in {"bootstrap", "discovery", "catalog", "diagnostics"}:
        return ""
    return first


def _walk_pending_choices(value: Any, *, path: str = "$"):
    if isinstance(value, dict):
        choices = value.get("choices")
        if isinstance(choices, dict):
            selected = choices.get("selected")
            available = _option_list(choices.get("available"))
            if selected in _EMPTY_SELECTIONS and available:
                yield path + ".choices", available

        selected = value.get("selected")
        available = _option_list(value.get("availableChoices"))
        if selected in _EMPTY_SELECTIONS and available:
            yield path, available

        for key, child in value.items():
            yield from _walk_pending_choices(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_pending_choices(child, path=f"{path}[{index}]")


def _walk_selected_choices(value: Any):
    if isinstance(value, dict):
        if "choice" in value:
            clean = _clean_option(value.get("choice"))
            if clean:
                yield clean
        if str(value.get("action") or "").casefold() == "dofsoption":
            clean = _clean_option(value.get("ind"))
            if clean:
                yield clean
        for child in value.values():
            yield from _walk_selected_choices(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_selected_choices(child)


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _explicit_branch_points(result: GameTestResult) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for mode in result.discovered_modes:
        if not isinstance(mode, dict):
            continue
        mode_id = str(mode.get("id") or "UNKNOWN")

        if mode.get("coverage_required") is True:
            required = _option_list(
                mode.get("required_options")
                if mode.get("required_options") is not None
                else mode.get("available")
            )
            covered = _option_list(
                mode.get("covered_options")
                if mode.get("covered_options") is not None
                else mode.get("selected_options")
            )
            if not covered:
                one = _clean_option(mode.get("selected_for_validation"))
                if one:
                    covered = [one]
            if required:
                points.append(
                    {
                        "source": "discovered_modes",
                        "mode_id": mode_id,
                        "signature": str(mode.get("branch_signature") or mode_id),
                        "required": required,
                        "covered": covered,
                    }
                )

        fs_required = _option_list(mode.get("fs_option_indices"))
        if fs_required:
            points.append(
                {
                    "source": "pragmatic_fso",
                    "mode_id": mode_id,
                    "signature": f"{mode_id}:doFSOption",
                    "required": fs_required,
                    "covered": _option_list(mode.get("fs_option_selected")),
                }
            )
    return points


def _artifact_branch_points(result: GameTestResult) -> list[dict[str, Any]]:
    root = Path(str(result.run_dir or ""))
    if not root.is_dir():
        return []

    selected_by_mode: dict[str, set[str]] = {}
    pending: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
    pragmatic_fso: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}

    for path in root.rglob("*.json"):
        if path.name in {"result.json", "path-coverage.json"}:
            continue
        mode_id = _mode_scope(root, path)
        if not mode_id:
            continue
        payload = _load_json(path)
        if payload is None:
            continue

        if (
            isinstance(payload, dict)
            and payload.get("schema") == "tester-spin/pragmatic-fso-selection/v1"
        ):
            required = _option_list(payload.get("option_indices"))
            selected = _clean_option(payload.get("selected_index"))
            if required:
                # The wire step in the artifact name distinguishes multiple FSO
                # prompts inside the same mode. Aggregate coverage by occurrence,
                # not merely by mode, so a second nested selector cannot be hidden
                # by options selected at the first one.
                signature = f"{mode_id}:doFSOption:{path.name}"
                key = (mode_id, signature, tuple(sorted(required)))
                item = pragmatic_fso.setdefault(
                    key,
                    {
                        "source": "pragmatic_fso_artifact",
                        "mode_id": mode_id,
                        "signature": signature,
                        "required": required,
                        "covered": set(),
                    },
                )
                if selected:
                    item["covered"].add(selected)
            continue

        lowered = path.name.casefold()
        if "request" in lowered:
            selected_by_mode.setdefault(mode_id, set()).update(_walk_selected_choices(payload))
            continue
        if "response" not in lowered and "summary" not in lowered and "attempt" not in lowered:
            continue

        for json_path, required in _walk_pending_choices(payload):
            key = (mode_id, json_path, tuple(sorted(required)))
            pending.setdefault(
                key,
                {
                    "source": "artifact_scan",
                    "mode_id": mode_id,
                    "signature": f"{mode_id}:{json_path}",
                    "required": required,
                },
            )

    points: list[dict[str, Any]] = []
    for item in pending.values():
        required = list(item["required"])
        covered = sorted(selected_by_mode.get(str(item["mode_id"]), set()).intersection(required))
        points.append({**item, "covered": covered})
    for item in pragmatic_fso.values():
        points.append(
            {
                **item,
                "covered": sorted(item["covered"]),
            }
        )
    return points


def build_path_coverage_report(result: GameTestResult) -> dict[str, Any]:
    points = _explicit_branch_points(result)
    explicit_signatures = {str(item["signature"]) for item in points}
    for item in _artifact_branch_points(result):
        if str(item["signature"]) not in explicit_signatures:
            points.append(item)

    normalized: list[dict[str, Any]] = []
    for item in points:
        required = list(dict.fromkeys(_option_list(item.get("required"))))
        covered = list(dict.fromkeys(_option_list(item.get("covered"))))
        missing = [value for value in required if value not in covered]
        normalized.append(
            {
                "source": str(item.get("source") or "unknown"),
                "mode_id": str(item.get("mode_id") or "UNKNOWN"),
                "signature": str(item.get("signature") or ""),
                "required": required,
                "covered": covered,
                "missing": missing,
                "complete": not missing,
            }
        )

    return {
        "schema": "tester-spin/path-coverage/v1",
        "provider": result.provider,
        "game": result.slug,
        "branch_points": normalized,
        "complete": all(item["complete"] for item in normalized),
        "missing_count": sum(len(item["missing"]) for item in normalized),
    }


def _append_error(result: GameTestResult, text: str) -> None:
    clean = str(text or "").strip()
    if not clean or clean in str(result.error or ""):
        return
    result.error = (str(result.error or "").strip() + " " + clean).strip()


def enforce_complete_path_coverage(
    result: GameTestResult,
    *,
    progress=None,
) -> GameTestResult:
    """Global invariant: an observed selectable branch cannot be silently skipped.

    This layer never invents provider requests. Provider adapters remain responsible
    for executing branches whose wire contract they understand. The neutral gate
    only verifies evidence and prevents OK when a discovered option is uncovered.
    """
    report = build_path_coverage_report(result)
    missing = [item for item in report["branch_points"] if item["missing"]]

    if result.run_dir:
        root = Path(result.run_dir)
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / "path-coverage.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    if missing:
        preview = "; ".join(
            f"{item['mode_id']} -> {item['missing']}" for item in missing[:8]
        )
        message = f"Cobertura de ramificaciones pendiente: {preview}."
        if result.status == "OK":
            result.status = "PARCIAL"
        if result.status not in {"ERROR", "CANCELADO"}:
            _append_error(result, message)
        if progress is not None:
            progress(message)
    elif report["branch_points"] and progress is not None:
        total = sum(len(item["required"]) for item in report["branch_points"])
        progress(f"Cobertura de ramificaciones completa: {total}/{total} opciones recorridas.")

    if result.run_dir:
        try:
            (Path(result.run_dir) / "result.json").write_text(
                json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
    return result
