"""Fail closed before publishing; never print a secret or a matching line."""

import argparse
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from package_release import source_files  # noqa: E402

KEY_PATTERN = re.compile(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{24,}")


def contains_secret(payload, actual_key=b""):
    return bool(actual_key and actual_key in payload or KEY_PATTERN.search(payload))


def git(*args):
    result = subprocess.run(["git", *args], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise ValueError("无法读取 Git 内容，凭证检查未完成。")
    return result.stdout


def check(*, archive=None, history=False):
    # Local exact match adds coverage for provider keys with an unfamiliar format.
    actual = os.environ.get("DEEPSEEK_API_KEY", "").strip().encode()
    inspected = 0
    def inspect(name, payload):
        nonlocal inspected
        inspected += 1
        if contains_secret(payload, actual):
            raise ValueError(f"拒绝发布：{name} 包含疑似凭证（值已隐藏）。")
        base = Path(name.replace("\\", "/")).name
        if base.startswith(".env") and base != ".env.example":
            raise ValueError("拒绝发布：检测到私有环境文件（内容已隐藏）。")
    for path in source_files():
        inspect(path.relative_to(ROOT).as_posix(), path.read_bytes())
    if (ROOT / ".git").exists():
        for raw_path in git("ls-files", "-z").split(b"\0"):
            if raw_path:
                name = raw_path.decode("utf-8")
                inspect(name, git("show", ":" + name))
        if history:
            seen = set()
            for commit in git("rev-list", "--all").splitlines():
                for record in git("ls-tree", "-rz", commit.decode()).split(b"\0"):
                    if not record:
                        continue
                    metadata, name = record.split(b"\t", 1)
                    _, kind, oid = metadata.split()
                    if kind != b"blob" or (oid, name) in seen:
                        continue
                    seen.add((oid, name))
                    inspect("history/" + name.decode("utf-8"), git("cat-file", "blob", oid.decode()))
    if archive:
        with zipfile.ZipFile(archive) as package:
            for item in package.infolist():
                if not item.is_dir():
                    inspect(item.filename, package.read(item))
    return inspected, bool(actual)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    try:
        count, exact = check(archive=args.archive, history=args.history)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1) from None
    print(f"Credential checks passed: {count} payloads; exact local-key check: {'enabled' if exact else 'unavailable (pattern checks active)'}. No key values printed.")


if __name__ == "__main__":
    main()
