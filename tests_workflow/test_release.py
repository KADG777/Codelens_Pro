"""The delivered archive must keep the Windows launch entry points executable."""

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def test_release_archive_preserves_launchers_and_matches_manifest(tmp_path):
    archive_path = tmp_path / "release.zip"
    completed = subprocess.run(
        [sys.executable, str(PROJECT / "scripts/package_release.py"), "--output", str(archive_path)],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("Codelens_Pro/MANIFEST.sha256.json"))
        assert ".gitattributes" in manifest
        assert "examples/1.py" not in manifest
        assert "sandbox_runtime/python-3.13.15-wasi_sdk-24.zip" in manifest
        for name, expected_hash in manifest.items():
            assert hashlib.sha256(archive.read(f"Codelens_Pro/{name}")).hexdigest() == expected_hash
        for name in ("start.cmd", "install.cmd", "launch.ps1", "setup.ps1"):
            packed = archive.read(f"Codelens_Pro/{name}")
            assert packed == (PROJECT / name).read_bytes()
            assert b"\n" not in packed.replace(b"\r\n", b"")
            if name.endswith(".cmd"):
                packed.decode("ascii")
                assert not packed.startswith(b"\xef\xbb\xbf")
            else:
                assert packed.startswith(b"\xef\xbb\xbf")
        assert not any("/.venv/" in name or name.endswith("/.env") for name in archive.namelist())
