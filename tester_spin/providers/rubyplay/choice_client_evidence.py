from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

_SCHEMA = "tester-spin/rubyplay-choice-client-evidence/v1"
_STRONG_MARKERS = ("PickMessageHandler", "SelectMessageHandler")
_ACTION_LITERAL_RE = re.compile(r"(['\"])(pick|select)\1", re.I)
_MAX_HITS_PER_SCRIPT = 24
_EXCERPT_RADIUS = 650


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _excerpt(text: str, offset: int, *, radius: int = _EXCERPT_RADIUS) -> str:
    start = max(0, int(offset) - max(1, int(radius)))
    end = min(len(text), int(offset) + max(1, int(radius)))
    return text[start:end]


def _hit(marker: str, text: str, offset: int) -> dict[str, Any]:
    excerpt = _excerpt(text, offset)
    return {
        "marker": str(marker),
        "offset": int(offset),
        "excerpt_sha256": _sha256(excerpt),
        "excerpt": excerpt,
    }


def _choice_hits(text: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for marker in _STRONG_MARKERS:
        start = 0
        while len(hits) < _MAX_HITS_PER_SCRIPT:
            offset = text.find(marker, start)
            if offset < 0:
                break
            key = (marker, offset)
            if key not in seen:
                hits.append(_hit(marker, text, offset))
                seen.add(key)
            start = offset + len(marker)

    if len(hits) < _MAX_HITS_PER_SCRIPT:
        for match in _ACTION_LITERAL_RE.finditer(text):
            if len(hits) >= _MAX_HITS_PER_SCRIPT:
                break
            action = str(match.group(2) or "").lower()
            excerpt = _excerpt(text, match.start(), radius=500)
            if "index" not in excerpt.casefold():
                continue
            marker = f"action:{action}:index-context"
            key = (marker, match.start())
            if key in seen:
                continue
            hits.append(_hit(marker, text, match.start()))
            seen.add(key)

    return sorted(hits, key=lambda row: (int(row["offset"]), str(row["marker"])))


def build_choice_client_evidence(
    scripts: list[tuple[str, str]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for url, text in scripts:
        source = str(text or "")
        rows.append(
            {
                "url": str(url or ""),
                "sha256": _sha256(source),
                "size": len(source.encode("utf-8", errors="replace")),
                "hits": _choice_hits(source),
            }
        )

    return {
        "schema": _SCHEMA,
        "scripts": rows,
        "summary": {
            "script_count": len(rows),
            "scripts_with_choice_evidence": sum(1 for row in rows if row["hits"]),
            "hit_count": sum(len(row["hits"]) for row in rows),
            "authority": "diagnostic-client-static-code-only",
            "promotes_domain": False,
        },
    }


def write_choice_client_evidence(
    artifact_dir: Path,
    scripts: list[tuple[str, str]],
) -> Path:
    root = Path(artifact_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "client-choice-evidence.json"
    target.write_text(
        json.dumps(
            build_choice_client_evidence(scripts),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


__all__ = [
    "build_choice_client_evidence",
    "write_choice_client_evidence",
]
