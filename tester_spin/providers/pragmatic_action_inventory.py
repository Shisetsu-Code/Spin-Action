from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tester_spin.models import GameTestResult


_ROOT_KINDS = {"SPIN", "ANTE_BET", "PURCHASE"}
_SOURCE = "pragmatic-doInit+runtime-evidence"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _mode_ids(rows: Any) -> list[str]:
    if not isinstance(rows, list):
        return []
    found: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("enabled") is False:
            continue
        kind = str(row.get("kind") or "").strip().upper()
        if kind not in _ROOT_KINDS:
            continue
        mode_id = str(row.get("id") or "").strip()
        if mode_id and mode_id not in found:
            found.append(mode_id)
    return sorted(found)


def _artifact_unknown(
    result: GameTestResult,
    *,
    reason: str,
    artifacts: dict[str, str],
) -> GameTestResult:
    result.structural_map["action_inventory"] = {
        "state": "UNKNOWN",
        "source": _SOURCE,
        "reason": reason,
        "root_actions": [],
        "missing_root_actions": [],
        "unexpected_root_actions": [],
        "artifacts": artifacts,
    }
    return result


def _persist_result(result: GameTestResult) -> None:
    run_dir = str(result.run_dir or "").strip()
    if not run_dir:
        return
    try:
        Path(run_dir, "result.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def annotate_pragmatic_action_inventory(result: GameTestResult) -> GameTestResult:
    """Annotate whether Pragmatic's gameplay action inventory is proven closed.

    Pragmatic root wager actions are authoritative only when they come from the
    current run's ``doInit``-derived mode catalog. Runtime continuations are not
    guessed: any unknown/unhandled response signature or uncovered selectable
    branch makes the inventory incomplete. Missing/invalid evidence stays UNKNOWN.

    This function deliberately does not alter ``result.status``. The normal
    Tester-Spin result and the independent Actions-lab verdict remain separate.
    """
    if not isinstance(result.structural_map, dict):
        result.structural_map = {}

    run_dir = str(result.run_dir or "").strip()
    artifacts = {
        "do_init": "discovery/doInit.response.json",
        "mode_catalog": "discovery/modes.json",
        "protocol_observations": "protocol-observations.json",
        "path_coverage": "path-coverage.json",
    }
    if not run_dir:
        _artifact_unknown(
            result,
            reason="Falta run_dir; no se puede demostrar el inventario Pragmatic de esta corrida.",
            artifacts=artifacts,
        )
        return result

    root = Path(run_dir)
    paths = {
        "do_init": root / artifacts["do_init"],
        "mode_catalog": root / artifacts["mode_catalog"],
        "protocol_observations": root / artifacts["protocol_observations"],
        "path_coverage": root / artifacts["path_coverage"],
    }
    for key in ("do_init", "mode_catalog", "protocol_observations", "path_coverage"):
        path = paths[key]
        if not path.is_file():
            _artifact_unknown(
                result,
                reason=f"Falta {artifacts[key]}; no se puede cerrar el inventario de acciones.",
                artifacts=artifacts,
            )
            _persist_result(result)
            return result

    try:
        do_init = _load_json(paths["do_init"])
        catalog = _load_json(paths["mode_catalog"])
        observations = _load_json(paths["protocol_observations"])
        coverage = _load_json(paths["path_coverage"])
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        _artifact_unknown(
            result,
            reason=f"Evidencia Pragmatic inválida o ilegible: {type(exc).__name__}: {exc}",
            artifacts=artifacts,
        )
        _persist_result(result)
        return result

    if not isinstance(do_init, dict):
        _artifact_unknown(
            result,
            reason="discovery/doInit.response.json no contiene un objeto JSON verificable.",
            artifacts=artifacts,
        )
        _persist_result(result)
        return result
    if not isinstance(catalog, dict) or catalog.get("schema") != "tester-spin/pragmatic-mode-catalog/v2":
        _artifact_unknown(
            result,
            reason="discovery/modes.json no tiene el schema Pragmatic esperado.",
            artifacts=artifacts,
        )
        _persist_result(result)
        return result
    if not isinstance(observations, dict):
        _artifact_unknown(
            result,
            reason="protocol-observations.json no contiene evidencia verificable.",
            artifacts=artifacts,
        )
        _persist_result(result)
        return result
    if not isinstance(coverage, dict) or coverage.get("schema") != "tester-spin/path-coverage/v1":
        _artifact_unknown(
            result,
            reason="path-coverage.json no tiene el schema de cobertura esperado.",
            artifacts=artifacts,
        )
        _persist_result(result)
        return result

    catalog_roots = _mode_ids(catalog.get("modes"))
    result_roots = _mode_ids(result.discovered_modes)
    missing_roots = sorted(set(catalog_roots) - set(result_roots))
    unexpected_roots = sorted(set(result_roots) - set(catalog_roots))

    inventory: dict[str, Any] = {
        "state": "UNKNOWN",
        "source": _SOURCE,
        "reason": "",
        "root_actions": catalog_roots,
        "missing_root_actions": missing_roots,
        "unexpected_root_actions": unexpected_roots,
        "artifacts": artifacts,
        "responses_analyzed": int(observations.get("responses_analyzed") or 0),
        "path_coverage_complete": coverage.get("complete") is True,
    }

    if not catalog_roots:
        inventory["state"] = "UNKNOWN"
        inventory["reason"] = (
            "doInit/modes.json no expone ninguna acción raíz habilitada; no se acepta cierre vacío."
        )
    elif missing_roots or unexpected_roots:
        inventory["state"] = "INCOMPLETE"
        details: list[str] = []
        if missing_roots:
            details.append(f"faltan acciones raíz={missing_roots}")
        if unexpected_roots:
            details.append(f"acciones raíz fuera de doInit={unexpected_roots}")
        inventory["reason"] = "Inventario raíz contradice doInit: " + "; ".join(details) + "."
    else:
        unknown_signatures = observations.get("unknown_signatures") or []
        unhandled_signatures = observations.get("unhandled_signatures") or []
        if not isinstance(unknown_signatures, list) or not isinstance(unhandled_signatures, list):
            inventory["state"] = "UNKNOWN"
            inventory["reason"] = "protocol-observations.json tiene listas de firmas inválidas."
        elif unknown_signatures or unhandled_signatures:
            inventory["state"] = "INCOMPLETE"
            inventory["reason"] = (
                "Existen firmas de protocolo desconocidas o sin handler; no se puede cerrar el inventario."
            )
            inventory["unknown_signature_count"] = len(unknown_signatures)
            inventory["unhandled_signature_count"] = len(unhandled_signatures)
        elif coverage.get("complete") is not True:
            inventory["state"] = "INCOMPLETE"
            inventory["reason"] = "Cobertura de ramas incompleta según path-coverage.json."
            inventory["missing_branch_count"] = int(coverage.get("missing_count") or 0)
        elif str(result.status or "").strip().upper() != "OK":
            inventory["state"] = "UNKNOWN"
            inventory["reason"] = (
                f"La evidencia estructural cerró, pero el runtime terminó en {result.status!r}; "
                "no se promueve el inventario a COMPLETE."
            )
        else:
            inventory["state"] = "COMPLETE"
            inventory["reason"] = (
                "doInit cerró las acciones raíz y la evidencia runtime no deja firmas ni ramas pendientes."
            )

    result.structural_map["action_inventory"] = inventory
    _persist_result(result)
    return result


__all__ = ["annotate_pragmatic_action_inventory"]
