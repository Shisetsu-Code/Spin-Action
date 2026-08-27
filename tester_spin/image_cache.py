from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

import requests

from tester_spin.models import Game


class ImageCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            }
        )

    @staticmethod
    def _safe(value: str) -> str:
        value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
        return value.strip("._-")[:120] or "image"

    def fetch(self, game: Game, timeout_s: float = 20.0) -> str:
        if not game.thumbnail_url:
            return ""
        provider_dir = self.root / self._safe(game.provider)
        provider_dir.mkdir(parents=True, exist_ok=True)

        parsed = urlparse(game.thumbnail_url)
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
            suffix = ""

        digest = hashlib.sha1(game.thumbnail_url.encode("utf-8")).hexdigest()[:12]
        base = f"{self._safe(game.slug)}_{digest}"

        existing = next(provider_dir.glob(base + ".*"), None)
        if existing and existing.is_file() and existing.stat().st_size > 0:
            return str(existing)

        response = self.session.get(game.thumbnail_url, timeout=timeout_s, stream=True)
        response.raise_for_status()
        if not suffix:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
            suffix = mimetypes.guess_extension(content_type) or ".img"
            if suffix == ".jpe":
                suffix = ".jpg"
        path = provider_dir / (base + suffix)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as fh:
            for chunk in response.iter_content(128 * 1024):
                if chunk:
                    fh.write(chunk)
        tmp.replace(path)
        return str(path)
