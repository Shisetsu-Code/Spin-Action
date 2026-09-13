from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from tester_spin.models import GameTestResult, utc_now_iso

Progress = Callable[[str], None]
RESULT_BRANCH = "tester-spin-runs"
RESULT_ROOT = "run-results"
EXPECTED_REPOSITORY = "shisetsu-code/tester-spin"
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
MAX_DOCUMENT_SOURCE_BYTES = 16 * 1024 * 1024
_TEXT_SUFFIXES = {".json", ".jsonl", ".txt", ".raw", ".log"}
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="github-result-publisher")


def _subprocess_window_kwargs() -> dict[str, Any]:
    """Keep helper processes invisible when Tester-Spin runs under pythonw on Windows."""
    if os.name != "nt":
        return {}

    kwargs: dict[str, Any] = {}
    create_no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    if create_no_window:
        kwargs["creationflags"] = create_no_window

    startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_factory is not None:
        startupinfo = startupinfo_factory()
        startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0) or 0)
        startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0) or 0)
        kwargs["startupinfo"] = startupinfo
    return kwargs


def _safe_component(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
    return clean[:140] or "unknown"


def _repo_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[1]
    try:
        proc = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            **_subprocess_window_kwargs(),
        )
    except Exception:
        return None
    value = proc.stdout.strip()
    return Path(value) if value else None


def _git(repo: Path, *args: str, input_text: str | None = None, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    merged.setdefault("GIT_AUTHOR_NAME", "Tester-Spin Results")
    merged.setdefault("GIT_AUTHOR_EMAIL", "tester-spin-results@local.invalid")
    merged.setdefault("GIT_COMMITTER_NAME", merged["GIT_AUTHOR_NAME"])
    merged.setdefault("GIT_COMMITTER_EMAIL", merged["GIT_AUTHOR_EMAIL"])
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=check,
        timeout=45,
        env=merged,
        **_subprocess_window_kwargs(),
    )


def _origin_is_expected(repo: Path) -> bool:
    try:
        remote = _git(repo, "remote", "get-url", "origin").stdout.strip().lower()
    except Exception:
        return False
    normalized = remote.removesuffix(".git").replace(":", "/")
    return EXPECTED_REPOSITORY in normalized


def _current_commit(repo: Path) -> str:
    try:
        return _git(repo, "rev-parse", "HEAD").stdout.strip()
    except Exception:
        return ""


def _read_artifact(path: Path) -> tuple[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.casefold() == ".json":
        try:
            return "json", json.loads(text)
        except Exception:
            pass
    return "text", text


def build_result_document(result: GameTestResult, *, source_commit: str = "") -> dict[str, Any]:
    root = Path(str(result.run_dir or ""))
    artifacts: dict[str, Any] = {}
    skipped: list[dict[str, Any]] = []
    consumed = 0
    if result.run_dir and root.is_dir():
        priority_names = {"result.json", "path-coverage.json", "sample-catalog.json", "profile.json", "flow-choice-coverage.json"}
        failed_roots = [Path(a.artifact_dir).resolve() for a in result.attempts
                        if a.artifact_dir and (not a.ok or not a.terminal or a.warning or a.error)]
        def priority(path: Path) -> tuple[int, str]:
            if path.name in priority_names:
                return (0, path.as_posix())
            if any(path.resolve().is_relative_to(folder) for folder in failed_roots):
                return (1, path.as_posix())
            return (2, path.as_posix())
        for path in sorted((item for item in root.rglob("*") if item.is_file()
                            and item.resolve().is_relative_to(root.resolve())), key=priority):
            if path.suffix.casefold() not in _TEXT_SUFFIXES:
                continue
            try:
                size = path.stat().st_size
                relative = path.relative_to(root).as_posix()
            except OSError:
                continue
            if size > MAX_TEXT_FILE_BYTES:
                skipped.append({"path": relative, "bytes": size, "reason": "file_limit"})
                continue
            if consumed + size > MAX_DOCUMENT_SOURCE_BYTES:
                skipped.append({"path": relative, "bytes": size, "reason": "document_limit"})
                continue
            try:
                encoding, value = _read_artifact(path)
            except OSError:
                skipped.append({"path": relative, "bytes": size, "reason": "read_error"})
                continue
            artifacts[relative] = {"encoding": encoding, "bytes": size, "value": value}
            consumed += size

    result_payload = result.to_dict()
    if result_payload.get("run_dir"):
        result_payload["run_dir"] = root.name
    return {
        "schema": "tester-spin/github-run-result/v1",
        "published_at": utc_now_iso(),
        "source_commit": source_commit,
        "provider": result.provider,
        "game": {"slug": result.slug, "name": result.game_name, "symbol": result.symbol, "url": result.game_url},
        "result": result_payload,
        "artifact_bytes_embedded": consumed,
        "artifacts": artifacts,
        "skipped_artifacts": skipped,
    }


def result_document_path(result: GameTestResult) -> str:
    return f"{RESULT_ROOT}/{_safe_component(result.provider)}/{_safe_component(result.slug)}.json"


def _remote_parent(repo: Path) -> str:
    _git(repo, "fetch", "--quiet", "origin", f"refs/heads/{RESULT_BRANCH}:refs/remotes/origin/{RESULT_BRANCH}", check=False)
    probe = _git(repo, "rev-parse", "--verify", f"refs/remotes/origin/{RESULT_BRANCH}", check=False)
    return probe.stdout.strip() if probe.returncode == 0 else ""


def _commit_document(repo: Path, path: str, content: str, message: str) -> str:
    for attempt in range(2):
        parent = _remote_parent(repo)
        with tempfile.TemporaryDirectory(prefix="tester-spin-publish-") as temp:
            env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
            _git(repo, "read-tree", parent, env=env) if parent else _git(repo, "read-tree", "--empty", env=env)
            blob = _git(repo, "hash-object", "-w", "--stdin", input_text=content).stdout.strip()
            _git(repo, "update-index", "--add", "--cacheinfo", "100644", blob, path, env=env)
            tree = _git(repo, "write-tree", env=env).stdout.strip()
            args = ["commit-tree", tree]
            if parent:
                args.extend(["-p", parent])
            args.extend(["-m", message])
            commit = _git(repo, *args).stdout.strip()
        pushed = _git(repo, "push", "--quiet", "origin", f"{commit}:refs/heads/{RESULT_BRANCH}", check=False)
        if pushed.returncode == 0:
            return commit
        if attempt == 1:
            raise RuntimeError((pushed.stderr or pushed.stdout or "git push failed").strip())
    raise RuntimeError("No se pudo publicar el resultado.")


def publish_result_to_github(result: GameTestResult, *, progress: Progress | None = None) -> str:
    if str(os.environ.get("TESTER_SPIN_PUBLISH_RESULTS", "1")).strip().casefold() in {"0", "false", "no", "off"}:
        return ""
    if not result.run_dir or not Path(result.run_dir).is_dir():
        return ""
    repo = _repo_root()
    if repo is None or not _origin_is_expected(repo):
        if progress is not None:
            progress("GitHub resultados: checkout/origin de Tester-Spin no disponible; publicación omitida.")
        return ""
    document = build_result_document(result, source_commit=_current_commit(repo))
    path = result_document_path(result)
    commit = _commit_document(repo, path, json.dumps(document, ensure_ascii=False, indent=2), f"Record {result.provider} result for {result.slug}")
    if progress is not None:
        progress(f"GitHub resultados: {RESULT_BRANCH}/{path} actualizado ({commit[:7]}).")
    return commit


def _publish_safely(result: GameTestResult, progress: Progress | None) -> str:
    try:
        return publish_result_to_github(result, progress=progress)
    except Exception as exc:
        if progress is not None:
            progress(f"GitHub resultados ERROR (no afecta la prueba): {type(exc).__name__}: {exc}")
        return ""


def queue_result_publish(result: GameTestResult, *, progress: Progress | None = None) -> Future[str]:
    return _EXECUTOR.submit(_publish_safely, result, progress)


def _shutdown() -> None:
    _EXECUTOR.shutdown(wait=True, cancel_futures=False)


atexit.register(_shutdown)

__all__ = ["RESULT_BRANCH", "build_result_document", "publish_result_to_github", "queue_result_publish", "result_document_path"]
