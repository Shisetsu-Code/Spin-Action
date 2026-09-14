from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable


SCHEMA = "tester-spin/local-replay-soak/v1"
_SAMPLE_SCHEMA = "tester-spin/sample-catalog/v2"


def _base_report(catalog: dict[str, Any], iterations: int) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "offline": True,
        "provider": str(catalog.get("provider") or ""),
        "game": str(catalog.get("game") or ""),
        "iterations_requested": max(0, int(iterations)),
        "iterations_executed": 0,
        "spin_corpus_samples": 0,
        "unique_spin_sequences": 0,
        "dominant_state_sequence_id": "",
        "stop_reason": "",
        "event": None,
        "note": (
            "Offline replay of already captured evidence only; this run cannot discover "
            "RNG outcomes that are absent from the source corpus."
        ),
    }


def _validated_spin_samples(catalog: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    groups = catalog.get("groups")
    if not isinstance(groups, list):
        return [], ""

    samples: list[dict[str, Any]] = []
    dominant_candidates: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        if str(group.get("mode_kind") or "").upper() != "SPIN":
            continue
        dominant = str(group.get("dominant_state_sequence_id") or "")
        if dominant:
            dominant_candidates.append(dominant)
        raw_samples = group.get("samples")
        if not isinstance(raw_samples, list):
            continue
        for sample in raw_samples:
            if not isinstance(sample, dict):
                continue
            if not bool(sample.get("validated")):
                continue
            samples.append(sample)

    dominant = dominant_candidates[0] if dominant_candidates else ""
    return samples, dominant


def replay_game_catalog(catalog: dict[str, Any], *, iterations: int = 3000) -> dict[str, Any]:
    """Replay captured SPIN samples locally without any network access.

    The function cycles deterministically through validated SPIN samples. It stops
    at the first captured state-sequence different from the catalog's dominant
    sequence. This is replay evidence, not a newly generated RNG outcome.
    """
    budget = max(0, int(iterations))
    report = _base_report(catalog, budget)

    if str(catalog.get("schema") or "") != _SAMPLE_SCHEMA:
        report["stop_reason"] = "INVALID_SAMPLE_CATALOG_SCHEMA"
        return report
    if not report["provider"] or not report["game"]:
        report["stop_reason"] = "INVALID_SAMPLE_CATALOG_IDENTITY"
        return report
    if budget == 0:
        report["stop_reason"] = "ZERO_ITERATION_BUDGET"
        return report

    samples, dominant = _validated_spin_samples(catalog)
    report["spin_corpus_samples"] = len(samples)
    report["dominant_state_sequence_id"] = dominant
    report["unique_spin_sequences"] = len(
        {
            str(sample.get("state_sequence_id") or "")
            for sample in samples
            if str(sample.get("state_sequence_id") or "")
        }
    )

    if not samples:
        report["stop_reason"] = "NO_VALID_SPIN_CORPUS"
        return report

    # If the source catalog did not publish a dominant sequence, use the first
    # non-empty captured sequence only for deterministic replay comparison. The
    # report keeps the value explicit rather than inferring feature semantics.
    if not dominant:
        dominant = next(
            (str(sample.get("state_sequence_id") or "") for sample in samples if sample.get("state_sequence_id")),
            "",
        )
        report["dominant_state_sequence_id"] = dominant

    for index in range(budget):
        sample = samples[index % len(samples)]
        report["iterations_executed"] = index + 1
        sequence_id = str(sample.get("state_sequence_id") or "")
        if sequence_id and dominant and sequence_id != dominant:
            report["stop_reason"] = "REPLAYED_STRUCTURAL_VARIANT"
            report["event"] = {
                "classification": "REPLAYED_STRUCTURAL_VARIANT",
                "sequence_id": sequence_id,
                "source_attempt": sample.get("attempt"),
                "artifact_dir": str(sample.get("artifact_dir") or ""),
                "observed_state_sequence": sample.get("observed_state_sequence", []),
                "evidence": sample.get("evidence", []),
                "note": (
                    "Variant was already present in captured evidence and was replayed "
                    "offline; it is not a newly generated live event."
                ),
            }
            return report

    report["stop_reason"] = "BUDGET_EXHAUSTED_NO_CORPUS_VARIANT"
    return report


def run_provider_replays(
    items: list[tuple[str, dict[str, Any]]],
    *,
    iterations: int = 3000,
    concurrency: int = 3,
    replay_fn: Callable[..., dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Replay one provider's games with at most three concurrent local workers."""
    worker_count = min(3, max(1, int(concurrency)))
    replay = replay_fn or replay_game_catalog
    if not items:
        return []

    indexed_results: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="replay-soak") as pool:
        futures = {
            pool.submit(replay, catalog, iterations=max(0, int(iterations))): index
            for index, (_key, catalog) in enumerate(items)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                indexed_results[index] = future.result()
            except BaseException as exc:
                key, catalog = items[index]
                indexed_results[index] = {
                    **_base_report(catalog, iterations),
                    "game": str(catalog.get("game") or key),
                    "stop_reason": "REPLAY_ERROR",
                    "error": f"{type(exc).__name__}: {exc}",
                }

    return [indexed_results[index] for index in range(len(items))]


__all__ = ["SCHEMA", "replay_game_catalog", "run_provider_replays"]
