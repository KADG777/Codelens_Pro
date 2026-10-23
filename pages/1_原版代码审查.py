"""Keep the Homework 1 Python/C++ review workspace available as a separate page."""

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[1] / "legacy_review.py"), run_name="__main__")
