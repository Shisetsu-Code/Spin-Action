"""Atomic snapshots and cross-process per-game merge transactions."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time

from .graph import StructuralMap


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # OS locks release on process death. Unlike lock-file existence, no stale lease.
    with open(path, "a+b") as stream:
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + 30
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Structural map lock busy")
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def persist_map(path: Path, incoming: StructuralMap, run_key: str) -> dict:
    with _lock(path.with_suffix(".lock")):
        result = incoming
        revisions = []
        if path.exists():
            previous = StructuralMap.load(json.loads(path.read_text(encoding="utf-8")))
            revisions = previous.data["metadata"].get("previous_revisions", [])
            if previous.scope == incoming.scope:
                if run_key not in previous.data["metadata"].get("merged_runs", []):
                    previous.merge(incoming)
                result = previous
            else:
                revision = path.with_name("game-structure-" + previous.key("revision") + ".json")
                # Windows filenames cannot contain ':'.
                revision = revision.with_name(revision.name.replace(":", "-"))
                atomic_write(revision, previous.to_dict())
                revisions = [*revisions, {"path": revision.name, "status": "stale; incompatible or unverified fingerprint"}]
        runs = result.data["metadata"].setdefault("merged_runs", [])
        if run_key not in runs:
            runs.append(run_key)
        result.data["metadata"]["previous_revisions"] = revisions
        payload = result.to_dict()
        atomic_write(path, payload)
        return payload
