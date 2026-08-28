from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from tester_spin.updater import MANIFEST_SCHEMA, UpdateManifest, Updater


class UpdaterTests(unittest.TestCase):
    def test_manifest_accepts_valid_package(self) -> None:
        manifest = UpdateManifest.from_dict(
            {
                "schema": MANIFEST_SCHEMA,
                "version": "2026.08.28.1",
                "url": "https://updates.example.test/tester-spin.zip",
                "sha256": "a" * 64,
                "entrypoint": "run.py",
            }
        )
        self.assertEqual(manifest.version, "2026.08.28.1")
        self.assertEqual(manifest.entrypoint, "run.py")

    def test_manifest_rejects_bad_hash(self) -> None:
        with self.assertRaises(ValueError):
            UpdateManifest.from_dict(
                {
                    "schema": MANIFEST_SCHEMA,
                    "version": "1.0.0",
                    "url": "https://updates.example.test/tester-spin.zip",
                    "sha256": "not-a-sha",
                }
            )

    def test_manifest_rejects_parent_entrypoint(self) -> None:
        with self.assertRaises(ValueError):
            UpdateManifest.from_dict(
                {
                    "schema": MANIFEST_SCHEMA,
                    "version": "1.0.0",
                    "url": "https://updates.example.test/tester-spin.zip",
                    "sha256": "b" * 64,
                    "entrypoint": "../run.py",
                }
            )

    def test_safe_extract_allows_normal_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "package.zip"
            destination = root / "out"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("run.py", "print('ok')\n")
                zf.writestr("tester_spin/example.py", "VALUE = 1\n")
            Updater.safe_extract_zip(archive, destination)
            self.assertTrue((destination / "run.py").exists())
            self.assertTrue((destination / "tester_spin" / "example.py").exists())

    def test_safe_extract_rejects_zip_slip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "package.zip"
            destination = root / "out"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escape.txt", "bad")
            with self.assertRaises(RuntimeError):
                Updater.safe_extract_zip(archive, destination)
            self.assertFalse((root / "escape.txt").exists())

    def test_sha256_file_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.bin"
            path.write_bytes(b"tester-spin")
            self.assertEqual(
                Updater.sha256_file(path),
                "fadaf006a584e2ba04287e4923c881d35c4da93505d1a399df2abfe3f46c705e",
            )


if __name__ == "__main__":
    unittest.main()
