from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from tester_spin.providers.bgaming.hyperhive_har import analyze_hyperhive_har


@dataclass(frozen=True, slots=True)
class HARQuality:
    path: str
    grade: str
    score: int
    operations: int
    plays: int
    spins: int
    purchases: int
    bootstrap_posts: int
    bytes: int

    @property
    def protocol_usable(self) -> bool:
        return self.operations > 0


def _post_payload(entry: dict[str, Any]) -> dict[str, Any] | None:
    request = entry.get("request")
    if not isinstance(request, dict):
        return None
    if str(request.get("method") or "").upper() != "POST":
        return None
    post_data = request.get("postData")
    if not isinstance(post_data, dict):
        return None
    text = post_data.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        payload = json.loads(text)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


@lru_cache(maxsize=256)
def _inspect_cached(path_text: str, size: int, mtime_ns: int) -> HARQuality:
    del mtime_ns
    path = Path(path_text)

    hyper = analyze_hyperhive_har(path)
    plays = int(hyper.play_count)
    purchases = len(hyper.purchase_features)
    spins = 0
    bootstrap_posts = 0

    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        log = payload.get("log") if isinstance(payload, dict) else None
        entries = log.get("entries") if isinstance(log, dict) else None
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                request_payload = _post_payload(entry)
                if not isinstance(request_payload, dict):
                    continue

                method = str(request_payload.get("method") or "").casefold()
                command = str(request_payload.get("command") or "").casefold()
                if method in {"init", "info"} or command in {"init", "info"}:
                    bootstrap_posts += 1

                if command == "spin":
                    spins += 1
                    options = request_payload.get("options")
                    if isinstance(options, dict) and options.get("purchased_feature"):
                        purchases += 1
    except Exception:
        pass

    operations = plays + spins
    if purchases > 0 and operations > 0:
        grade = "HAR_WITH_PURCHASE"
        score = 300_000 + purchases * 1_000 + operations
    elif operations > 0:
        grade = "HAR_WITH_PLAY"
        score = 200_000 + operations
    elif bootstrap_posts > 0:
        grade = "HAR_BOOTSTRAP_ONLY"
        score = 100_000 + bootstrap_posts
    else:
        grade = "HAR_NETWORK_ONLY"
        score = 1_000

    return HARQuality(
        path=str(path),
        grade=grade,
        score=score,
        operations=operations,
        plays=plays,
        spins=spins,
        purchases=purchases,
        bootstrap_posts=bootstrap_posts,
        bytes=size,
    )


def inspect_har(path: Path | str | None) -> HARQuality | None:
    if path is None:
        return None
    candidate = Path(path)
    try:
        stat = candidate.stat()
    except OSError:
        return None
    if not candidate.is_file() or stat.st_size <= 0:
        return None
    if candidate.name.casefold().endswith(".partial.har"):
        return None
    return _inspect_cached(
        str(candidate.resolve()),
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )


def select_best_har(game_dir: Path) -> Path | None:
    """Select the richest HAR by observed protocol evidence, never by filename.

    This fixes the old behavior where analysis/browser.har always won even when it
    contained only bootstrap/init while a manually supplied HAR contained real
    spin/play traffic.
    """
    root = Path(game_dir)
    if not root.exists():
        return None

    ranked: list[tuple[int, int, int, int, str, Path]] = []
    for path in root.rglob("*.har"):
        quality = inspect_har(path)
        if quality is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        ranked.append(
            (
                quality.score,
                quality.operations,
                quality.purchases,
                int(stat.st_mtime_ns),
                str(path).casefold(),
                path,
            )
        )

    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][-1]


__all__ = ["HARQuality", "inspect_har", "select_best_har"]
