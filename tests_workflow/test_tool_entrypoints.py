"""User-facing module commands must import correctly without sending model requests."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module", [
    "scripts.package_release",
    "scripts.evaluation.evaluate_workflow",
    "scripts.experiments.compare_optimized",
    "scripts.demo.run_demo",
])
def test_tool_module_help_works_without_model_credentials(module):
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        cwd=PROJECT,
        env={**os.environ, "DEEPSEEK_API_KEY": "", "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout and "--help" in result.stdout
