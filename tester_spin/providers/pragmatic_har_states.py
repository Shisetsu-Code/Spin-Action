from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult, SpinAttempt
from tester_spin.providers.base import Progress
from tester_spin.providers.pragmatic import HttpBootstrap, _fmt, _int
from tester_spin.providers.pragmatic_endpoint import (
    MAX_WIRE_STEPS,
    PragmaticProvider as _EndpointPragmaticProvider,
)
from tester_spin.providers.pragmatic_modes import PragmaticMode, PragmaticModeCatalog, discover_modes


def parse_fs_options(fields: dict[str, str]) -> list[dict[str, Any]]:
    """Parse Pragmatic fs_opt using fs_opt_mask while preserving provider indices."""
    raw = str(fields.get("fs_opt") or "").strip()
    if not raw:
        return []
    mask = [part.strip() for part in str(fields.get("fs_opt_mask") or "").split(",") if part.strip()]
    options: list[dict[str, Any]] = []
    for provider_index, chunk in enumerate(raw.split("~")):
        chunk = chunk.strip()
        if not chunk:
            continue
        values = [part.strip() for part in chunk.split(",")]
        first = values[0] if values else ""
        try:
            if float(first) < 0:
                continue
        except Exception:
            pass
        mapped = {
            mask[i]: values[i]
            for i in range(min(len(mask), len(values)))
        }
        options.append(
            {
                "index": provider_index,
                "raw": chunk,
                "values": values,
                "fields": mapped,
            }
        )
    return options


def choose_fs_option_index(
    options: list[dict[str, Any]],
    *,
    repetition: int,
    preferred: int | None = None,
) -> int:
    indices = [int(item["index"]) for item in options]
    if not indices:
        raise ValueError("fs_opt no contiene opciones seleccionables")
    if preferred is not None:
        if preferred not in indices:
            raise ValueError(f"FS option ind={preferred} no está entre {indices}")
        return preferred
    return indices[(max(1, int(repetition)) - 1) % len(indices)]


def build_fs_option_request(
    *,
    symbol: str,
    mgckey: str,
    index: int,
    counter: int,
    option_index: int,
) -> dict[str, str]:
    return {
        "action": "doFSOption",
        "symbol": symbol,
        "ind": str(option_index),
        "index": str(index),
        "counter": str(counter),
        "repeat": "0",
        "mgckey": mgckey,
    }


def build_mystery_scatter_request(
    *,
    symbol: str,
    mgckey: str,
    index: int,
    counter: int,
    s_info: str,
) -> dict[str, str]:
    return {
        "action": "doMysteryScatter",
        "symbol": symbol,
        "sInfo": s_info or "n",
        "index": str(index),
        "counter": str(counter),
        "repeat": "0",
        "mgckey": mgckey,
    }


class PragmaticProvider(_EndpointPragmaticProvider):
    """HAR-grounded continuation states layered on the endpoint-first adapter."""

    def _test_mode_once(
        self,
        game: Game,
        *,
        symbol: str,
        cver: str | None,
        mode: PragmaticMode,
        catalog: PragmaticModeCatalog,
        attempt_number: int,
        repetition: int,
        run_root: Path,
        timeout_s: float,
        fs_option_index: int | None = None,
    ) -> SpinAttempt:
        attempt_root = run_root / mode.id / f"attempt-{repetition:04d}"
        attempt_root.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        bootstrap: HttpBootstrap | None = None

        try:
            bootstrap = self._http_bootstrap(game.url, symbol, cver, catalog.base_bet, timeout_s)
            self._write_http_bootstrap(attempt_root, bootstrap)

            index = (
                _int(bootstrap.calibration_response.get("index"))
                or _int(bootstrap.init_response.get("index"))
                or 1
            ) + 1
            counter = (
                _int(bootstrap.calibration_response.get("counter"))
                or _int(bootstrap.init_response.get("counter"))
                or 1
            ) + 1

            fields = dict(bootstrap.spin_template)
            fields.update(
                {
                    "action": "doSpin",
                    "symbol": symbol,
                    "c": _fmt(catalog.base_coin),
                    "l": _fmt(catalog.base_scale),
                    "sInfo": "n",
                    "bl": str(mode.provider_bl or 0),
                    "index": str(index),
                    "counter": str(counter),
                    "repeat": fields.get("repeat", "0") or "0",
                    "mgckey": bootstrap.mgckey,
                }
            )
            if mode.kind == "PURCHASE":
                fields["pur"] = str(mode.provider_pur)
            else:
                fields.pop("pur", None)

            last_s_info = str(fields.get("sInfo") or "n")
            status_code, _, last, _ = self._post_and_store(
                bootstrap,
                fields,
                attempt_root,
                step=0,
                label="entry",
                timeout_s=timeout_s,
            )
            if status_code >= 400:
                raise RuntimeError(f"HTTP {status_code}")
            error = self._server_error(last)
            if error:
                raise RuntimeError(f"server error={error}")

            wire_steps = 1
            terminal = False
            warning = ""
            in_bonus = False

            while wire_steps < MAX_WIRE_STEPS:
                na = str(last.get("na") or "").strip().lower()

                if na == "b":
                    in_bonus = True
                    index = (_int(last.get("index")) or index) + 1
                    counter = (_int(last.get("counter")) or counter) + 1
                    bonus_fields = {
                        "symbol": symbol,
                        "action": "doBonus",
                        "index": str(index),
                        "counter": str(counter),
                        "repeat": "0",
                        "mgckey": bootstrap.mgckey,
                    }
                    status_code, _, last, _ = self._post_and_store(
                        bootstrap,
                        bonus_fields,
                        attempt_root,
                        step=wire_steps,
                        label="bonus",
                        timeout_s=timeout_s,
                    )
                    wire_steps += 1
                    if status_code >= 400:
                        raise RuntimeError(f"doBonus HTTP {status_code}")
                    error = self._server_error(last)
                    if error:
                        raise RuntimeError(f"doBonus server error={error}")
                    continue

                if na in {"cb", "bc"} or (na == "c" and in_bonus):
                    index = (_int(last.get("index")) or index) + 1
                    counter = (_int(last.get("counter")) or counter) + 1
                    collect_bonus_fields = {
                        "symbol": symbol,
                        "action": "doCollectBonus",
                        "index": str(index),
                        "counter": str(counter),
                        "repeat": "0",
                        "mgckey": bootstrap.mgckey,
                    }
                    status_code, _, last, _ = self._post_and_store(
                        bootstrap,
                        collect_bonus_fields,
                        attempt_root,
                        step=wire_steps,
                        label="collect-bonus",
                        timeout_s=timeout_s,
                    )
                    wire_steps += 1
                    if status_code >= 400:
                        raise RuntimeError(f"doCollectBonus HTTP {status_code}")
                    error = self._server_error(last)
                    if error:
                        raise RuntimeError(f"doCollectBonus server error={error}")
                    next_na = str(last.get("na") or "").strip().lower()
                    if next_na in {"", "s"} and not self._feature_active(last):
                        terminal = True
                        break
                    continue

                if na == "c":
                    index = (_int(last.get("index")) or index) + 1
                    counter = (_int(last.get("counter")) or counter) + 1
                    collect_fields = {
                        "symbol": symbol,
                        "action": "doCollect",
                        "index": str(index),
                        "counter": str(counter),
                        "repeat": "0",
                        "mgckey": bootstrap.mgckey,
                    }
                    status_code, _, last, _ = self._post_and_store(
                        bootstrap,
                        collect_fields,
                        attempt_root,
                        step=wire_steps,
                        label="collect",
                        timeout_s=timeout_s,
                    )
                    wire_steps += 1
                    if status_code >= 400:
                        raise RuntimeError(f"doCollect HTTP {status_code}")
                    error = self._server_error(last)
                    if error:
                        raise RuntimeError(f"doCollect server error={error}")
                    terminal = True
                    break

                if na == "fso" and last.get("fs_opt"):
                    options = parse_fs_options(last)
                    try:
                        selected = choose_fs_option_index(
                            options,
                            repetition=repetition,
                            preferred=fs_option_index,
                        )
                    except ValueError as exc:
                        warning = f"na='fso' no automatizable: {exc}; RAW preservado"
                        break

                    self._write_json(
                        attempt_root / f"fso-selection-{wire_steps:03d}.json",
                        {
                            "schema": "tester-spin/pragmatic-fso-selection/v1",
                            "source": "HAR-grounded doFSOption",
                            "option_count": len(options),
                            "option_indices": [int(item["index"]) for item in options],
                            "selected_index": selected,
                            "fs_opt_mask": str(last.get("fs_opt_mask") or ""),
                            "fs_opt": str(last.get("fs_opt") or ""),
                            "options": options,
                        },
                    )
                    index = (_int(last.get("index")) or index) + 1
                    counter = (_int(last.get("counter")) or counter) + 1
                    option_fields = build_fs_option_request(
                        symbol=symbol,
                        mgckey=bootstrap.mgckey,
                        index=index,
                        counter=counter,
                        option_index=selected,
                    )
                    status_code, _, last, _ = self._post_and_store(
                        bootstrap,
                        option_fields,
                        attempt_root,
                        step=wire_steps,
                        label=f"fs-option-{selected}",
                        timeout_s=timeout_s,
                    )
                    wire_steps += 1
                    if status_code >= 400:
                        raise RuntimeError(f"doFSOption HTTP {status_code}")
                    error = self._server_error(last)
                    if error:
                        raise RuntimeError(f"doFSOption server error={error}")
                    continue

                if na == "m" and (last.get("mb") or last.get("psym")):
                    index = (_int(last.get("index")) or index) + 1
                    counter = (_int(last.get("counter")) or counter) + 1
                    mystery_fields = build_mystery_scatter_request(
                        symbol=symbol,
                        mgckey=bootstrap.mgckey,
                        index=index,
                        counter=counter,
                        s_info=last_s_info,
                    )
                    status_code, _, last, _ = self._post_and_store(
                        bootstrap,
                        mystery_fields,
                        attempt_root,
                        step=wire_steps,
                        label="mystery-scatter",
                        timeout_s=timeout_s,
                    )
                    wire_steps += 1
                    if status_code >= 400:
                        raise RuntimeError(f"doMysteryScatter HTTP {status_code}")
                    error = self._server_error(last)
                    if error:
                        raise RuntimeError(f"doMysteryScatter server error={error}")
                    continue

                if na == "s" and self._feature_active(last):
                    index = (_int(last.get("index")) or index) + 1
                    counter = (_int(last.get("counter")) or counter) + 1
                    continuation = dict(bootstrap.spin_template)
                    continuation.update(
                        {
                            "action": "doSpin",
                            "symbol": symbol,
                            "c": _fmt(catalog.base_coin),
                            "l": _fmt(catalog.base_scale),
                            "sInfo": "n",
                            "bl": str(mode.provider_bl or 0),
                            "index": str(index),
                            "counter": str(counter),
                            "repeat": "0",
                            "mgckey": bootstrap.mgckey,
                        }
                    )
                    continuation.pop("pur", None)
                    last_s_info = str(continuation.get("sInfo") or "n")
                    status_code, _, last, _ = self._post_and_store(
                        bootstrap,
                        continuation,
                        attempt_root,
                        step=wire_steps,
                        label="continuation-spin",
                        timeout_s=timeout_s,
                    )
                    wire_steps += 1
                    if status_code >= 400:
                        raise RuntimeError(f"continuation doSpin HTTP {status_code}")
                    error = self._server_error(last)
                    if error:
                        raise RuntimeError(f"continuation doSpin server error={error}")
                    continue

                if na in {"", "s"}:
                    terminal = True
                    break

                warning = f"estado de continuación no automatizado: na={na!r}; RAW preservado"
                break

            if not terminal and not warning and wire_steps >= MAX_WIRE_STEPS:
                warning = f"límite de {MAX_WIRE_STEPS} pasos alcanzado; RAW preservado"

            elapsed_ms = (time.monotonic() - started) * 1000.0
            attempt = SpinAttempt(
                number=attempt_number,
                ok=True,
                mode_id=mode.id,
                mode_kind=mode.kind,
                provider_bl=mode.provider_bl,
                provider_pur=mode.provider_pur,
                status_code=status_code,
                elapsed_ms=elapsed_ms,
                symbol=symbol,
                endpoint=bootstrap.endpoint,
                na=str(last.get("na") or ""),
                terminal=terminal,
                wire_steps=wire_steps,
                warning=warning,
                artifact_dir=str(attempt_root),
            )
            self._write_json(attempt_root / "attempt.json", attempt.to_dict())
            return attempt

        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            attempt = SpinAttempt(
                number=attempt_number,
                ok=False,
                mode_id=mode.id,
                mode_kind=mode.kind,
                provider_bl=mode.provider_bl,
                provider_pur=mode.provider_pur,
                elapsed_ms=elapsed_ms,
                symbol=symbol,
                endpoint="" if bootstrap is None else bootstrap.endpoint,
                error=f"{type(exc).__name__}: {exc}",
                artifact_dir=str(attempt_root),
            )
            self._write_json(attempt_root / "attempt.json", attempt.to_dict())
            return attempt
        finally:
            if bootstrap is not None:
                bootstrap.session.close()

    @staticmethod
    def _collect_fso_coverage(run_root: Path) -> dict[str, dict[str, set[int]]]:
        coverage: dict[str, dict[str, set[int]]] = {}
        for path in run_root.rglob("fso-selection-*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                relative = path.relative_to(run_root)
                mode_id = relative.parts[0]
                item = coverage.setdefault(mode_id, {"available": set(), "selected": set()})
                item["available"].update(int(value) for value in payload.get("option_indices") or [])
                if payload.get("selected_index") is not None:
                    item["selected"].add(int(payload["selected_index"]))
            except Exception:
                continue
        return coverage

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
        if stop_event.is_set() or not result.run_dir:
            return result

        run_root = Path(result.run_dir)
        coverage = self._collect_fso_coverage(run_root)
        missing_by_mode = {
            mode_id: sorted(values["available"] - values["selected"])
            for mode_id, values in coverage.items()
            if values["available"] - values["selected"]
        }

        for mode_dict in result.discovered_modes:
            mode_id = str(mode_dict.get("id") or "")
            values = coverage.get(mode_id)
            if values:
                mode_dict["fs_option_indices"] = sorted(values["available"])
                mode_dict["fs_option_selected"] = sorted(values["selected"])
                mode_dict["fs_option_protocol"] = "doFSOption(ind)"

        if not missing_by_mode:
            if coverage:
                for mode_id, values in coverage.items():
                    progress(
                        f"{mode_id}: cobertura FSO {len(values['selected'])}/"
                        f"{len(values['available'])} opciones."
                    )
            self._write_json(run_root / "result.json", result.to_dict())
            self._record_last_test_in_game_json(game, result)
            return result

        progress(
            "FSO: se detectaron opciones internas no cubiertas; "
            "se ejecutarán sesiones adicionales para probar cada ind."
        )

        planned_extra = sum(len(indices) for indices in missing_by_mode.values())
        result.requested_spins += planned_extra
        expansion_errors: list[str] = []

        try:
            discovery = self._browser_bootstrap(
                game.url,
                timeout_s=max(60.0, timeout_s),
                progress=progress,
            )
            catalog = discover_modes(discovery.init_response, requested_base_bet=self.base_bet)
            modes = {mode.id: mode for mode in catalog.enabled()}
            attempt_number = max((attempt.number for attempt in result.attempts), default=0)

            next_rep: dict[str, int] = {}
            for mode_id in missing_by_mode:
                existing = []
                mode_root = run_root / mode_id
                if mode_root.exists():
                    for path in mode_root.glob("attempt-*"):
                        try:
                            existing.append(int(path.name.split("-", 1)[1]))
                        except Exception:
                            pass
                next_rep[mode_id] = max(existing, default=0) + 1

            for mode_id, missing_indices in missing_by_mode.items():
                mode = modes.get(mode_id)
                if mode is None:
                    expansion_errors.append(f"{mode_id}: modo ya no aparece en doInit")
                    continue
                for option_index in missing_indices:
                    if stop_event.is_set():
                        expansion_errors.append("expansión FSO detenida por el usuario")
                        break
                    attempt_number += 1
                    repetition = next_rep[mode_id]
                    next_rep[mode_id] += 1
                    progress(
                        f"{mode_id}/FS_OPTION_{option_index}: "
                        f"probando opción interna ind={option_index}"
                    )
                    attempt = self._test_mode_once(
                        game,
                        symbol=discovery.symbol,
                        cver=discovery.cver,
                        mode=mode,
                        catalog=catalog,
                        attempt_number=attempt_number,
                        repetition=repetition,
                        run_root=run_root,
                        timeout_s=timeout_s,
                        fs_option_index=option_index,
                    )
                    result.attempts.append(attempt)
                    if attempt.ok:
                        suffix = f"; {attempt.warning}" if attempt.warning else ""
                        progress(
                            f"{mode_id}/FS_OPTION_{option_index}: respuesta OK "
                            f"HTTP {attempt.status_code}, na={attempt.na!r}, "
                            f"steps={attempt.wire_steps}{suffix}"
                        )
                    else:
                        expansion_errors.append(
                            f"{mode_id}/FS_OPTION_{option_index}: {attempt.error}"
                        )
                        progress(
                            f"{mode_id}/FS_OPTION_{option_index}: ERROR {attempt.error}"
                        )
        except Exception as exc:
            expansion_errors.append(f"{type(exc).__name__}: {exc}")

        coverage = self._collect_fso_coverage(run_root)
        for mode_dict in result.discovered_modes:
            mode_id = str(mode_dict.get("id") or "")
            values = coverage.get(mode_id)
            if values:
                mode_dict["fs_option_indices"] = sorted(values["available"])
                mode_dict["fs_option_selected"] = sorted(values["selected"])
                mode_dict["fs_option_protocol"] = "doFSOption(ind)"

        responded = sum(1 for attempt in result.attempts if attempt.ok)
        completed = sum(1 for attempt in result.attempts if attempt.ok and attempt.terminal)
        pending = sum(1 for attempt in result.attempts if attempt.ok and not attempt.terminal)
        failed = sum(1 for attempt in result.attempts if not attempt.ok)
        missing_attempts = max(0, result.requested_spins - len(result.attempts))

        result.successful_spins = completed
        result.failed_spins = failed + missing_attempts
        if expansion_errors:
            extra_error = "; ".join(expansion_errors)
            result.error = f"{result.error}; {extra_error}".strip("; ")
        if pending or result.failed_spins or expansion_errors:
            result.status = "PARCIAL" if completed else "ERROR"
        else:
            result.status = "OK"

        for mode_id, values in coverage.items():
            progress(
                f"{mode_id}: cobertura FSO {len(values['selected'])}/"
                f"{len(values['available'])} opciones."
            )
        progress(
            f"Resumen tras expansión FSO: respondieron={responded}/{result.requested_spins}, "
            f"completados={completed}/{result.requested_spins}, "
            f"pendientes={pending}, errores={result.failed_spins}"
        )

        self._write_json(run_root / "result.json", result.to_dict())
        self._record_last_test_in_game_json(game, result)
        return result
