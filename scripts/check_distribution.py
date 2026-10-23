"""Check the public source selection and the shipped WASI runtime without API calls."""

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from package_release import source_files  # noqa: E402
from workflow.wasi_runtime import ARCHIVE_NAME, ARCHIVE_SHA256, runtime_available  # noqa: E402

REQUIRED = {
    "app.py", ".github/workflows/ci.yml", ".gitignore", ".env.example",
    "requirements-dev.txt", "requirements-lock.txt", "README.md",
    "docs/GITHUB_UPLOAD.md", "docs/CI.md", "scripts/check_distribution.py",
    "start.cmd", "install.cmd", "sandbox_runtime/LICENSE",
    f"sandbox_runtime/{ARCHIVE_NAME}",
}
PRIVATE_DIRS = {
    ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    "data", "reports", "dist",
}


def check_names(names):
    paths = set(names)
    if missing := REQUIRED - paths:
        raise ValueError(f"Missing distribution files: {sorted(missing)}")
    for name in paths:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError(f"Unsafe archive path: {name}")
        if any(part in PRIVATE_DIRS or part.startswith((".test-", ".tmp-")) for part in path.parts):
            raise ValueError(f"Private/generated file included: {name}")
        if path.name.startswith(".env") and path.name != ".env.example":
            raise ValueError(f"Environment credentials included: {name}")
        if name == "examples/1.py" or path.suffix in {".db", ".sqlite", ".sqlite3", ".pem", ".key"}:
            raise ValueError(f"Personal data or credentials included: {name}")


def check_source():
    check_names(path.relative_to(ROOT).as_posix() for path in source_files())
    runtime = ROOT / "sandbox_runtime" / ARCHIVE_NAME
    if hashlib.sha256(runtime.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("Portable runtime archive checksum mismatch")
    if not runtime_available():
        raise ValueError("Wasmtime must be installed; functional tests must not silently skip")


def check_archive(path):
    prefix = "Codelens_Pro/"
    with zipfile.ZipFile(path) as archive:
        entries = archive.namelist()
        if len(entries) != len(set(entries)) or any(not name.startswith(prefix) for name in entries):
            raise ValueError("Invalid archive root or duplicate entries")
        manifest = json.loads(archive.read(prefix + "MANIFEST.sha256.json"))
        check_names(manifest)
        if set(entries) != {prefix + name for name in manifest} | {prefix + "MANIFEST.sha256.json"}:
            raise ValueError("Archive contains missing or unlisted files")
        for name, expected in manifest.items():
            if hashlib.sha256(archive.read(prefix + name)).hexdigest() != expected:
                raise ValueError(f"Archive checksum mismatch: {name}")
        runtime = archive.read(prefix + "sandbox_runtime/" + ARCHIVE_NAME)
        if hashlib.sha256(runtime).hexdigest() != ARCHIVE_SHA256:
            raise ValueError("Packaged runtime checksum mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    check_source()
    if args.archive:
        check_archive(args.archive)
    print("Distribution checks passed (source selection, required files, runtime SHA-256).")


if __name__ == "__main__":
    main()
