from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

APP_NAME = "Tester-Spin"
DEFAULT_REPO_URL = "https://github.com/Shisetsu-Code/Tester-Spin.git"
DEFAULT_BRANCH = "main"
MANIFEST_SCHEMA = "tester-spin-update/v1"
Status = Callable[[str], None]


def _local_appdata() -> Path:
    raw = os.environ.get("LOCALAPPDATA")
    if raw:
        return Path(raw) / APP_NAME
    return Path.home() / ".tester-spin"


def _launcher_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _detect_existing_data_dir() -> Path:
    launcher = _launcher_dir()
    candidates = (launcher / "data", launcher.parent / "data")
    for candidate in candidates:
        if (candidate / "tester-spin.sqlite3").exists():
            return candidate.resolve()
    return (_local_appdata() / "data").resolve()


@dataclass(slots=True)
class UpdaterConfig:
    mode: str = "git"
    repo_url: str = DEFAULT_REPO_URL
    branch: str = DEFAULT_BRANCH
    manifest_url: str = ""
    data_dir: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "UpdaterConfig":
        return cls(
            mode=str(value.get("mode") or "git").strip().lower(),
            repo_url=str(value.get("repo_url") or DEFAULT_REPO_URL).strip(),
            branch=str(value.get("branch") or DEFAULT_BRANCH).strip(),
            manifest_url=str(value.get("manifest_url") or "").strip(),
            data_dir=str(value.get("data_dir") or "").strip(),
        )


@dataclass(slots=True)
class UpdateManifest:
    version: str
    url: str
    sha256: str
    entrypoint: str = "run.py"

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "UpdateManifest":
        if str(value.get("schema") or "") != MANIFEST_SCHEMA:
            raise ValueError("manifest schema no soportado")
        version = str(value.get("version") or "").strip()
        url = str(value.get("url") or "").strip()
        digest = str(value.get("sha256") or "").strip().lower()
        entrypoint = str(value.get("entrypoint") or "run.py").strip().replace("\\", "/")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", version):
            raise ValueError("version inválida en manifest")
        if not url.startswith(("https://", "http://")):
            raise ValueError("URL de paquete inválida")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("sha256 inválido")
        if not entrypoint or entrypoint.startswith("/") or ".." in Path(entrypoint).parts:
            raise ValueError("entrypoint inválido")
        return cls(version=version, url=url, sha256=digest, entrypoint=entrypoint)


class Updater:
    def __init__(self, *, home: Path | None = None) -> None:
        self.home = (home or _local_appdata()).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.config_path = self.home / "updater.json"
        self.runtime_root = self.home / "runtime"
        self.versions_root = self.home / "versions"
        self.venv_root = self.home / "venv"
        self.browser_root = self.home / "playwright"
        self.current_path = self.home / "current.json"
        self.config = self._load_or_create_config()

    @property
    def data_dir(self) -> Path:
        raw = self.config.data_dir.strip()
        path = Path(raw) if raw else _detect_existing_data_dir()
        path = path.expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _load_or_create_config(self) -> UpdaterConfig:
        if self.config_path.exists():
            try:
                value = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return UpdaterConfig.from_dict(value)
            except Exception:
                pass
        config = UpdaterConfig(data_dir=str(_detect_existing_data_dir()))
        self._write_json_atomic(self.config_path, asdict(config))
        return config

    @staticmethod
    def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.stdout.strip()

    @staticmethod
    def _find_executable(name: str) -> str | None:
        return shutil.which(name)

    def prepare_runtime(self, status: Status) -> Path:
        mode = self.config.mode
        if mode == "manifest":
            if not self.config.manifest_url:
                raise RuntimeError("updater.json usa mode=manifest pero manifest_url está vacío")
            return self._prepare_manifest_runtime(status)
        if mode != "git":
            raise RuntimeError(f"modo de actualización no soportado: {mode}")
        return self._prepare_git_runtime(status)

    def _prepare_git_runtime(self, status: Status) -> Path:
        git = self._find_executable("git")
        repo = self.runtime_root / "repo"
        if not git:
            if (repo / "run.py").exists():
                status("Git no está disponible; usando la última versión instalada.")
                return repo
            raise RuntimeError("Git no está instalado o no está en PATH")

        self.runtime_root.mkdir(parents=True, exist_ok=True)
        if not (repo / ".git").exists():
            status("Instalando Tester-Spin por primera vez...")
            staging = self.runtime_root / "repo.new"
            shutil.rmtree(staging, ignore_errors=True)
            try:
                self._run(
                    [git, "clone", "--single-branch", "--branch", self.config.branch, self.config.repo_url, str(staging)]
                )
                if repo.exists():
                    shutil.rmtree(repo, ignore_errors=True)
                os.replace(staging, repo)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        else:
            status("Buscando actualizaciones en GitHub...")
            try:
                self._run([git, "-C", str(repo), "remote", "set-url", "origin", self.config.repo_url])
                self._run([git, "-C", str(repo), "fetch", "--prune", "origin", self.config.branch])
                before = self._run([git, "-C", str(repo), "rev-parse", "HEAD"])
                target = self._run([git, "-C", str(repo), "rev-parse", f"origin/{self.config.branch}"])
                if before != target:
                    status("Actualización encontrada; sincronizando main...")
                    self._run([git, "-C", str(repo), "checkout", "-B", self.config.branch, f"origin/{self.config.branch}"])
                    self._run([git, "-C", str(repo), "reset", "--hard", f"origin/{self.config.branch}"])
                    self._run([git, "-C", str(repo), "clean", "-fd"])
                    status(f"Actualizado a {target[:12]}.")
                else:
                    status("Tester-Spin ya está actualizado.")
            except Exception as exc:
                if (repo / "run.py").exists():
                    status(f"No se pudo actualizar ({type(exc).__name__}); usando la última versión válida.")
                    return repo
                raise

        if not (repo / "run.py").exists():
            raise RuntimeError("la copia runtime no contiene run.py")
        return repo

    def _read_current_manifest_runtime(self) -> Path | None:
        try:
            value = json.loads(self.current_path.read_text(encoding="utf-8"))
            version = str(value.get("version") or "")
            entrypoint = str(value.get("entrypoint") or "run.py")
            root = self.versions_root / version
            if (root / entrypoint).exists():
                return root
        except Exception:
            return None
        return None

    def _fetch_json(self, url: str) -> dict[str, object]:
        request = urllib.request.Request(url, headers={"User-Agent": "Tester-Spin-Updater/1"})
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("manifest no es un objeto JSON")
        return value

    def _prepare_manifest_runtime(self, status: Status) -> Path:
        fallback = self._read_current_manifest_runtime()
        try:
            status("Consultando canal de actualizaciones...")
            manifest = UpdateManifest.from_dict(self._fetch_json(self.config.manifest_url))
        except Exception as exc:
            if fallback is not None:
                status(f"Canal no disponible ({type(exc).__name__}); usando la última versión válida.")
                return fallback
            raise

        target = self.versions_root / manifest.version
        if (target / manifest.entrypoint).exists():
            self._write_json_atomic(
                self.current_path,
                {"version": manifest.version, "entrypoint": manifest.entrypoint},
            )
            status(f"Tester-Spin {manifest.version} ya está instalado.")
            return target

        self.versions_root.mkdir(parents=True, exist_ok=True)
        status(f"Descargando Tester-Spin {manifest.version}...")
        with tempfile.TemporaryDirectory(prefix="tester-spin-update-") as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            archive = temp_dir / "package.zip"
            request = urllib.request.Request(manifest.url, headers={"User-Agent": "Tester-Spin-Updater/1"})
            with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
            actual = self.sha256_file(archive)
            if actual != manifest.sha256:
                raise RuntimeError("SHA-256 del paquete no coincide con el manifest")

            staging = self.versions_root / f".{manifest.version}.staging"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            self.safe_extract_zip(archive, staging)
            if not (staging / manifest.entrypoint).exists():
                shutil.rmtree(staging, ignore_errors=True)
                raise RuntimeError(f"el paquete no contiene {manifest.entrypoint}")
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            os.replace(staging, target)

        self._write_json_atomic(
            self.current_path,
            {"version": manifest.version, "entrypoint": manifest.entrypoint},
        )
        self._cleanup_old_versions(keep=3)
        status(f"Actualizado a Tester-Spin {manifest.version}.")
        return target

    @staticmethod
    def sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def safe_extract_zip(archive: Path, destination: Path) -> None:
        destination = destination.resolve()
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                candidate = (destination / member.filename).resolve()
                try:
                    candidate.relative_to(destination)
                except ValueError as exc:
                    raise RuntimeError(f"ruta insegura dentro del ZIP: {member.filename}") from exc
            zf.extractall(destination)

    def _cleanup_old_versions(self, *, keep: int) -> None:
        if not self.versions_root.exists():
            return
        dirs = [item for item in self.versions_root.iterdir() if item.is_dir() and not item.name.startswith(".")]
        dirs.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for old in dirs[max(1, keep):]:
            shutil.rmtree(old, ignore_errors=True)

    def _find_system_python(self) -> Path:
        if not getattr(sys, "frozen", False) and Path(sys.executable).name.lower().startswith("python"):
            return Path(sys.executable).resolve()
        py = self._find_executable("py")
        if py:
            output = self._run([py, "-3", "-c", "import sys;print(sys.executable)"])
            if output:
                return Path(output.splitlines()[-1].strip()).resolve()
        python = self._find_executable("python")
        if python:
            return Path(python).resolve()
        raise RuntimeError("Python 3 no está instalado o no está en PATH")

    def ensure_environment(self, app_dir: Path, status: Status) -> tuple[Path, dict[str, str]]:
        system_python = self._find_system_python()
        venv_python = self.venv_root / "Scripts" / "python.exe" if os.name == "nt" else self.venv_root / "bin" / "python"
        venv_pythonw = self.venv_root / "Scripts" / "pythonw.exe" if os.name == "nt" else venv_python
        if not venv_python.exists():
            status("Preparando entorno de Python...")
            self.venv_root.parent.mkdir(parents=True, exist_ok=True)
            self._run([str(system_python), "-m", "venv", str(self.venv_root)])

        requirements = app_dir / "requirements.txt"
        req_hash = self.sha256_file(requirements) if requirements.exists() else "none"
        marker = self.venv_root / ".requirements.sha256"
        installed_hash = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""

        env = os.environ.copy()
        env["TESTER_SPIN_DATA_ROOT"] = str(self.data_dir)
        env["TESTER_SPIN_APP_HOME"] = str(self.home)
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(self.browser_root)

        if installed_hash != req_hash:
            status("Actualizando dependencias...")
            self._run([str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", "pip"], env=env)
            if requirements.exists():
                self._run(
                    [str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(requirements)],
                    env=env,
                )
            status("Verificando Chromium de Playwright...")
            try:
                self._run([str(venv_python), "-m", "playwright", "install", "chromium"], env=env)
            except Exception:
                # Endpoint-first operation can continue even when Chromium is not
                # available; DOM fallback will report its own error if ever needed.
                pass
            marker.write_text(req_hash + "\n", encoding="utf-8")

        return (venv_pythonw if venv_pythonw.exists() else venv_python), env

    def launch(self, app_dir: Path, status: Status) -> subprocess.Popen[bytes]:
        python_exe, env = self.ensure_environment(app_dir, status)
        entrypoint = app_dir / "run.py"
        if not entrypoint.exists():
            raise RuntimeError(f"no existe {entrypoint}")
        status("Abriendo Tester-Spin...")
        return subprocess.Popen(
            [str(python_exe), str(entrypoint)],
            cwd=str(app_dir),
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
