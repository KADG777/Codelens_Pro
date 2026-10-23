"""Build a reproducible archive; personal runtime data and credentials stay out."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXCLUDED_DIRS = {
    ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".test-tmp",
    "data", "reports", "dist",
}
ALLOWED_SUFFIXES = {".py", ".md", ".txt", ".toml", ".json", ".cmd", ".ps1", ".cpp", ".h", ".yml", ".html", ".js", ".css"}
EXCLUDED_FILES = {"examples/1.py"}  # Personal upload inherited from the Homework 1 snapshot.


def release_bytes(path: Path) -> bytes:
    """Keep Windows entry points executable even after an editor changes EOLs."""
    data = path.read_bytes()
    if path.suffix.lower() == ".cmd":
        text = data.decode("ascii")
        return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n").encode("ascii")
    if path.suffix.lower() == ".ps1":
        text = data.decode("utf-8-sig")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        return normalized.encode("utf-8-sig")
    return data


def source_files():
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if relative.as_posix() in EXCLUDED_FILES:
            continue
        hidden_build_dir = any(
            part.startswith(".") and part != ".github" for part in relative.parts[:-1]
        )
        if not path.is_file() or hidden_build_dir or any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.name.startswith(".env") and path.name != ".env.example":
            continue
        if relative.as_posix() == "sandbox_runtime/python-3.13.15-wasi_sdk-24.zip":
            yield path
            continue
        if path.name in {".env.example", ".gitignore", ".gitattributes", "LICENSE"} or path.suffix in ALLOWED_SUFFIXES:
            yield path


def main():
    parser = argparse.ArgumentParser(description="打包不含凭证、缓存或运行记录的源码版")
    parser.add_argument("--output", type=Path, default=ROOT.parent / "Codelens_Pro.zip")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    files = list(source_files())
    payloads = {path.relative_to(ROOT).as_posix(): release_bytes(path) for path in files}
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}
    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in payloads.items():
            archive.writestr(f"Codelens_Pro/{name}", data)
        archive.writestr(
            "Codelens_Pro/MANIFEST.sha256.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    print(f"已打包 {len(files)} 个源码文件：{args.output.resolve()}")
    print(f"大小：{args.output.stat().st_size / 1024 / 1024:.2f} MB")


if __name__ == "__main__":
    main()
