"""Bounded source loading, with Python encoding declarations and C++ UTF-8."""

import io
import tokenize
from pathlib import Path

from review_agent.config import MAX_SOURCE_BYTES
from review_agent.languages import detect_language


def decode_source(data: bytes, filename: str = "review.py") -> str:
    language = detect_language(filename)
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError(f"文件过大，请提交不超过 {MAX_SOURCE_BYTES // 1000} KB 的源代码文件。")
    try:
        if language == "cpp":
            return data.decode("utf-8-sig")
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        return data.decode(encoding)
    except (UnicodeError, SyntaxError, LookupError):
        raise ValueError(
            "无法解码文件，请保存为 UTF-8；Python 文件也支持有效的编码声明。"
        ) from None


def read_source_file(path: Path) -> str:
    with path.open("rb") as handle:
        return decode_source(handle.read(MAX_SOURCE_BYTES + 1), path.name)
