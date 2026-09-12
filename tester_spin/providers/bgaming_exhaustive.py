from __future__ import annotations

import itertools
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.bgaming import BGamingProvider as _BGamingProvider
from tester_spin.providers.bgaming import execution as _execution


MAX_OPTION_COMBINATIONS = 128
_OVERRIDE_LOCAL = threading.local()
_ORIGINAL_DISCOVER_PROFILE = _execution.discover_profile


def _same_option(left: Any, right: Any) -> bool:
    return (type(left) is type(right) and left == right) or str(left) == str(right)


def _apply_profile_override(profile):
    override = getattr(_OVERRIDE_LOCAL, "spin_options", None)
    if not isinstance(override, dict) or not override:
        return profile
    for field, selected in override.items():
        choices = profile.spin_option_choices.get(field)
        if not isinstance(choices, list) or not choices:
            raise ValueError(f"BGaming exhaustive: selector {field!r} sin dominio descubierto.")
        match = next((value for value in choices if _same_option(value, selected)), None)
        if match is None:
            raise ValueError(
                f"BGaming exhaustive: {field}={selected!r} no está en {choices!r}."
            )
        profile.spin_options[field] = match
        profile.evidence.append(f"exhaustive.override.{field}={match}")
    return profile


def _discover_profile_with_override(*args, **kwargs):
    return _apply_profile_override(_ORIGINAL_DISCOVER_PROFILE(*args, **kwargs))


if getattr(_execution.discover_profile, "__name__", "") != "_discover_profile_with_override":
    _execution.discover_profile = _discover_profile_with_override


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_profile(result: GameTestResult) -> dict[str, Any]:
    root = Path(str(result.run_dir or ""))
    path = root / "profile.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _choice_domains(profile: dict[str, Any]) -> list[tuple[str, list[Any]]]:
    raw = profile.get("spin_option_choices")
    if not isinstance(raw, dict):
        return []
    out: list[tuple[str, list[Any]]] = []
    for field in sorted(raw):
        values = raw.get(field)
        if not isinstance(values, list):
            continue
        clean: list[Any] = []
        for value in values:
            if value in (None, ""):
                continue
            if not any(_same_option(value, item) for item in clean):
                clean.append(value)
        if len(clean) > 1:
            out.append((str(field), clean))
    return out


def _matrix(domains: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:
    if not domains:
        return [{}]
    names = [name for name, _values in domains]
    return [
        dict(zip(names, values))
        for values in itertools.product(*(values for _name, values in domains))
    ]


def _label(combo: dict[str, Any]) -> str:
    if not combo:
        return "BASE"
    return "|".join(
        f"{field}={json.dumps(combo[field], ensure_ascii=False, sort_keys=True)}"
        for field in sorted(combo)
    )


def _safe_label(label: str) -> str:
    out = []
    for ch in label:
        out.append(ch if ch.isalnum() or ch in {"-", "_", "."} else "_")
    return "".join(out)[:180] or "BASE"


def _base_combo(profile: dict[str, Any], domains) -> dict[str, Any]:
    selected = profile.get("spin_options")
    if not isinstance(selected, dict):
        selected = {}
    combo: dict[str, Any] = {}
    for field, values in domains:
        current = selected.get(field)
        match = next((value for value in values if _same_option(value, current)), None)
        combo[field] = values[0] if match is None else match
    return combo


def _complete(result: GameTestResult) -> bool:
    return (
        result.status == "OK"
        and result.requested_spins > 0
        and result.successful_spins == result.requested_spins
        and result.failed_spins == 0
        and bool(result.attempts)
        and all(attempt.ok and attempt.terminal for attempt in result.attempts)
    )


def _remap_attempt_paths(result: GameTestResult, old_root: Path, new_root: Path) -> None:
    for attempt in result.attempts:
        if not attempt.artifact_dir:
            continue
        path = Path(attempt.artifact_dir)
        try:
            rel = path.relative_to(old_root)
        except ValueError:
            continue
        attempt.artifact_dir = str(new_root / rel)


def _move_run(result: GameTestResult, target: Path) -> None:
    source = Path(str(result.run_dir or ""))
    if not source.is_dir():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    old = source
    shutil.move(str(source), str(target))
    _remap_attempt_paths(result, old, target)
    result.run_dir = str(target)


class BGamingProvider(_BGamingProvider):
    """BGaming package provider plus exhaustive additionalSpinOptions traversal."""

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        result = super().test_game(
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )
        if result.status in {"ERROR", "CANCELADO", "SIN_DEMO"} or not result.run_dir:
            return result

        profile = _load_profile(result)
        domains = _choice_domains(profile)
        if not domains:
            return result

        combinations = _matrix(domains)
        base_combo = _base_combo(profile, domains)
        base_label = _label(base_combo)
        required = [_label(combo) for combo in combinations]
        covered: set[str] = {base_label} if _complete(result) else set()

        original_root = Path(result.run_dir)
        master_root = original_root.with_name(
            original_root.name + f"-exhaustive-{time.time_ns() % 1_000_000_000:09d}"
        )
        _move_run(result, master_root)
        added_requested = 0
        added_successes = 0
        branch_errors: list[str] = []
        executed = 0

        for combo in combinations:
            label = _label(combo)
            if label == base_label:
                continue
            if stop_event.is_set():
                branch_errors.append("detenido por el usuario")
                break
            if executed >= MAX_OPTION_COMBINATIONS:
                branch_errors.append(
                    f"matriz excede guard de {MAX_OPTION_COMBINATIONS} combinaciones"
                )
                break
            executed += 1
            progress(f"[{game.name}] BGaming opciones dinámicas: probando {label}.")
            _OVERRIDE_LOCAL.spin_options = dict(combo)
            try:
                sub = super().test_game(
                    game,
                    spins=max(1, int(spins)),
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=progress,
                )
            except Exception as exc:
                branch_errors.append(f"{label}: {type(exc).__name__}: {exc}")
                continue
            finally:
                _OVERRIDE_LOCAL.spin_options = None

            sub_root = Path(str(sub.run_dir or ""))
            target = master_root / "branch-runs" / _safe_label(label)
            if sub_root.is_dir():
                _move_run(sub, target)
            added_requested += sub.requested_spins
            added_successes += sub.successful_spins
            suffix = _safe_label(label).upper()
            for attempt in sub.attempts:
                attempt.mode_id = f"{attempt.mode_id}__OPTIONS_{suffix}"
                attempt.mode_kind = f"{attempt.mode_kind}_OPTION_VARIANT"
                result.attempts.append(attempt)
            if _complete(sub):
                covered.add(label)
            else:
                branch_errors.append(f"{label}: {sub.status} {sub.error}".strip())

        result.requested_spins += added_requested
        result.successful_spins += added_successes
        result.failed_spins = max(0, result.requested_spins - result.successful_spins)
        result.discovered_modes.append(
            {
                "id": "BGAMING_SPIN_OPTION_MATRIX",
                "kind": "CHOICE_BRANCH",
                "observed": True,
                "executable": True,
                "wire_command": "additionalSpinOptions",
                "coverage_required": True,
                "branch_signature": "BGAMING:additionalSpinOptions:matrix",
                "dimensions": [
                    {"field": name, "values": values}
                    for name, values in domains
                ],
                "required_options": required,
                "covered_options": sorted(covered),
            }
        )
        missing = [value for value in required if value not in covered]
        if missing and result.status == "OK":
            result.status = "PARCIAL"
        if missing or branch_errors:
            details = []
            if missing:
                details.append("faltan=" + ", ".join(missing[:20]))
            if branch_errors:
                details.append("errores=" + " | ".join(branch_errors[:8]))
            message = "BGaming cobertura de additionalSpinOptions incompleta: " + "; ".join(details) + "."
            if message not in str(result.error or ""):
                result.error = (str(result.error or "").strip() + " " + message).strip()
        _write_json(master_root / "result.json", result.to_dict())
        return result


# Existing wiring tests intentionally require the active BGaming provider to be
# identified as package-backed. This subclass preserves that public boundary while
# adding only the exhaustive traversal layer above the package implementation.
BGamingProvider.__module__ = "tester_spin.providers.bgaming"

__all__ = ["BGamingProvider"]
