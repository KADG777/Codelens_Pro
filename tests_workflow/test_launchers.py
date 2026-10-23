"""Run the real Windows batch entry points, including paths with Unicode and spaces."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows CMD entry-point regression")


def run_batch(script, arguments, *, cwd, env=None, timeout=30):
    """Pass CMD its documented outer quote pair without list2cmdline escaping it."""
    command_processor = os.environ.get("COMSPEC", "cmd.exe")
    # Python's argv quoting is suitable for individual arguments. CMD's /s /c
    # command must retain its own outer quotes around the quoted script path.
    command = (
        f'{subprocess.list2cmdline([command_processor])} /d /s /c '
        f'""{script}" {subprocess.list2cmdline(arguments)}"'
    )
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # A broken -CheckOnly must not leave an accidentally launched server.
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        pytest.fail("The batch entry point did not finish in check-only mode")
    return process.returncode, output.decode("utf-8", errors="replace")


@pytest.mark.parametrize("name", ["start.cmd", "install.cmd"])
def test_batch_entry_point_is_ascii_crlf_without_bom(name):
    raw = (PROJECT / name).read_bytes()

    assert raw and not raw.startswith(b"\xef\xbb\xbf")
    raw.decode("ascii")
    assert b"\r\n" in raw
    without_crlf = raw.replace(b"\r\n", b"")
    assert b"\r" not in without_crlf and b"\n" not in without_crlf


@pytest.mark.parametrize(
    "wrapper,target", [("start.cmd", "launch.ps1"), ("install.cmd", "setup.ps1")]
)
def test_real_wrapper_forwards_arguments_and_exit_code_from_unicode_directory(
    tmp_path, wrapper, target
):
    copied_project = tmp_path / "中文 空格" / "CodeLens Flow"
    copied_project.mkdir(parents=True)
    outside = tmp_path / "external working directory"
    outside.mkdir()
    copied_wrapper = copied_project / wrapper
    shutil.copyfile(PROJECT / wrapper, copied_wrapper)
    marker = tmp_path / f"{wrapper}.json"
    stub = """param(
    [switch]$CheckOnly,
    [int]$Port = 8503,
    [switch]$Headless,
    [string]$Message = ''
)
$record = [ordered]@{
    root = $PSScriptRoot
    check_only = [bool]$CheckOnly
    port = $Port
    headless = [bool]$Headless
    message = $Message
}
$json = $record | ConvertTo-Json -Compress
[IO.File]::WriteAllText($env:CODELENS_TEST_LAUNCH_RECORD, $json, [Text.UTF8Encoding]::new($false))
exit 37
"""
    (copied_project / target).write_text(stub, encoding="utf-8-sig", newline="\r\n")
    child_env = {**os.environ, "CODELENS_TEST_LAUNCH_RECORD": str(marker)}

    return_code, output = run_batch(
        copied_wrapper,
        ["-CheckOnly", "-Port", "8517", "-Headless", "-Message", "kept as one argument"],
        cwd=outside,
        env=child_env,
    )

    assert return_code == 37, output
    assert marker.is_file(), output
    record = json.loads(marker.read_text(encoding="utf-8"))
    assert Path(record["root"]).resolve() == copied_project.resolve()
    assert record["check_only"] is True
    assert record["port"] == 8517
    assert record["headless"] is True
    assert record["message"] == "kept as one argument"


def test_source_start_check_only_runs_through_cmd_without_starting_server(tmp_path):
    return_code, output = run_batch(
        PROJECT / "start.cmd",
        ["-CheckOnly", "-Port", "8517", "-Headless"],
        cwd=tmp_path,
    )
    assert return_code == 0, output


def test_source_setup_check_only_does_not_create_environment_or_install(tmp_path):
    local_environment = PROJECT / ".venv"
    existed_before = local_environment.exists()
    child_env = {**os.environ, "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    return_code, output = run_batch(
        PROJECT / "install.cmd", ["-CheckOnly"], cwd=tmp_path, env=child_env
    )

    assert return_code == 0, output
    assert local_environment.exists() is existed_before
