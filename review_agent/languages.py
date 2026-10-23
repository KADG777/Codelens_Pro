"""One source of truth for file types and language labels."""

from pathlib import PurePosixPath
from typing import Literal

LanguageName = Literal["python", "cpp"]
LANGUAGE_LABELS = {"python": "Python", "cpp": "C++"}
PYTHON_EXTENSIONS = {".py"}
CPP_EXTENSIONS = {".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h"}
UPLOAD_EXTENSIONS = ["py", "cpp", "cc", "cxx", "hpp", "hh", "hxx", "h"]


def detect_language(filename: str) -> LanguageName:
    extension = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    if extension in PYTHON_EXTENSIONS:
        return "python"
    if extension in CPP_EXTENSIONS:
        return "cpp"
    raise ValueError("支持 Python (.py) 和 C++ (.cpp/.cc/.cxx/.hpp/.hh/.hxx/.h) 文件。")
